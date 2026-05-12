"""
Gateway Stream Test
───────────────────
sending videos per frame to Gateway HTTP /predict 

Usage:
  python3 test/validate_video.py
  python3 test/validate_video.py --video samples/burst_traffic.mp4 --gateway http://172.18.0.3:30708
  python3 test/validate_video.py --video samples/simple_test.mp4 --gateway http://localhost:5000 --out samples/st_annotated.mp4
"""

import io
import sys
import time
import base64
import argparse
import logging
from pathlib import Path

import cv2
import numpy as np
import requests
from PIL import Image

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent


def encode_jpeg(frame_bgr: np.ndarray, quality: int = 85) -> bytes:
    img_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
    buf = io.BytesIO()
    Image.fromarray(img_rgb).save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def decode_b64_jpeg(b64_str: str) -> np.ndarray:
    # strip data URI prefix if present
    if "," in b64_str:
        b64_str = b64_str.split(",", 1)[1]
    data = base64.b64decode(b64_str)
    pil = Image.open(io.BytesIO(data)).convert("RGB")
    return cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)


def run(video_path: str, gateway_url: str, max_frames: int, out_path: str | None):
    predict_url = gateway_url.rstrip("/") + "/predict"

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        log.error(f"Cannot open video: {video_path}")
        sys.exit(1)

    fps   = cap.get(cv2.CAP_PROP_FPS) or 25.0
    w     = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h     = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    log.info(f"Video : {video_path}  {w}x{h} @ {fps:.1f}fps  {total} frames")
    log.info(f"Target: {predict_url}")

    writer = None
    if out_path:
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        writer = cv2.VideoWriter(out_path, fourcc, fps, (w, h))

    stats = {
        "frames": 0, "errors": 0,
        "persons": 0, "vehicles": 0, "other": 0,
        "latency_ms": [],
    }
    ## use a single Session for connection pooling, avoiding overhead of new TCP connection for each frame
    session = requests.Session()

    frame_idx = 0
    while True:
        ret, frame_bgr = cap.read()
        if not ret or (max_frames and frame_idx >= max_frames):
            break

        jpeg_bytes = encode_jpeg(frame_bgr)

        t0 = time.time()
        try:
            resp = session.post(
                predict_url,
                files={"image": ("frame.jpg", jpeg_bytes, "image/jpeg")},
                timeout=30, # seconds
            )
            resp.raise_for_status()
        except requests.RequestException as e:
            log.error(f"Frame {frame_idx}: HTTP error — {e}")
            stats["errors"] += 1
            frame_idx += 1
            continue
        ## full end-to-end latency 
        rtt_ms = (time.time() - t0) * 1000

        data = resp.json()
        latency = rtt_ms

        ped  = data.get("PersonPoseHandler", {})
        veh  = data.get("VehicleCountHandler", {})
        persons  = ped.get("person_count", 0)
        vehicles = veh.get("current_total", 0)
        other    = len(data.get("unhandled", []))

        stats["frames"]    += 1
        stats["persons"]   += persons
        stats["vehicles"]  += vehicles
        stats["other"]     += other
        stats["latency_ms"].append(latency)

        if writer and data.get("annotated_img"):
            annotated = decode_b64_jpeg(data["annotated_img"])
            cv2.putText(
                annotated,
                f"F{frame_idx:04d} | persons:{persons} vehicles:{vehicles} | {latency:.0f}ms",
                (8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2,
            )
            writer.write(annotated)

        if frame_idx % 25 == 0:
            log.info(
                f"Frame {frame_idx:>4d}/{total}: persons={persons} vehicles={vehicles} "
                f"other={other}  latency={latency:.0f}ms"
            )

        frame_idx += 1

    cap.release()
    if writer:
        writer.release()
        log.info(f"Annotated video saved → {out_path}")

    if stats["errors"]:
        log.warning(f"{stats['errors']} frames failed")

    lat = stats["latency_ms"]
    print("\n" + "=" * 50)
    print("  GATEWAY STREAM SUMMARY")
    print("=" * 50)
    print(f"  Frames processed : {stats['frames']}")
    print(f"  Errors           : {stats['errors']}")
    print(f"  Total persons    : {stats['persons']}")
    print(f"  Total vehicles   : {stats['vehicles']}")
    print(f"  Other detections : {stats['other']}")
    if lat:
        print(f"  Avg latency      : {sum(lat)/len(lat):.1f} ms")
        print(f"  Min / Max latency: {min(lat):.1f} / {max(lat):.1f} ms")
    print("=" * 50)


def main():
    default_video = str(PROJECT_ROOT / "samples" / "burst_traffic.mp4")

    p = argparse.ArgumentParser(description="Stream video frames to Gateway /predict")
    p.add_argument("--video",      default=default_video,
                   help=f"Input video (default: {default_video})")
    p.add_argument("--gateway",    default="http://localhost:5000",
                   help="Gateway base URL (default: http://localhost:5000)")
    p.add_argument("--max-frames", type=int, default=0,
                   help="Max frames to send (0 = all)")
    p.add_argument("--out",        default=None,
                   help="Save annotated output video to this path")
    args = p.parse_args()

    run(args.video, args.gateway, args.max_frames, args.out)


if __name__ == "__main__":
    main()
