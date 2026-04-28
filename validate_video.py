"""
Video Dataset Validation Script
────────────────────────────────
Two modes:
  --local   : run YOLOv8 directly (no gRPC services needed)
  --grpc    : send frames to Router gRPC service (requires services up)

Usage:
  # Local YOLO validation (default, no services needed):
  python validate_video.py --video datasets/traffic_car.mp4 --local

  # gRPC pipeline validation:
  python validate_video.py --video datasets/traffic_car.mp4 --grpc --addr localhost:50051
"""

import io
import sys
import time
import argparse
import logging

import cv2
import numpy as np
from PIL import Image

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)


# ─── helpers ────────────────────────────────────────────────────────────────

def encode_jpeg(frame_bgr: np.ndarray, quality: int = 85) -> bytes:
    img_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
    pil = Image.fromarray(img_rgb)
    buf = io.BytesIO()
    pil.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def decode_jpeg(data: bytes) -> np.ndarray:
    pil = Image.open(io.BytesIO(data)).convert("RGB")
    return cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)


# ─── local mode ─────────────────────────────────────────────────────────────

def run_local(video_path: str, max_frames: int, out_path: str | None):
    from ultralytics import YOLO

    model = YOLO("yolov8s.pt")
    VEHICLE_CLASSES = {"car", "truck", "bus", "motorcycle"}

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        log.error(f"Cannot open video: {video_path}")
        sys.exit(1)

    fps = cap.get(cv2.CAP_PROP_FPS) or 12.5
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    log.info(f"Video: {w}x{h} @ {fps:.1f}fps  total={total} frames")

    writer = None
    if out_path:
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        writer = cv2.VideoWriter(out_path, fourcc, fps, (w, h))

    stats = {"frames": 0, "persons": 0, "vehicles": 0, "other": 0, "latency_ms": []}

    frame_idx = 0
    while True:
        ret, frame = cap.read()
        if not ret or (max_frames and frame_idx >= max_frames):
            break

        t0 = time.time()
        results = model(frame, verbose=False)[0]
        latency = (time.time() - t0) * 1000

        persons = vehicles = other = 0
        for box in results.boxes:
            cls = results.names[int(box.cls)]
            if cls == "person":
                persons += 1
            elif cls in VEHICLE_CLASSES:
                vehicles += 1
            else:
                other += 1

        stats["frames"] += 1
        stats["persons"] += persons
        stats["vehicles"] += vehicles
        stats["other"] += other
        stats["latency_ms"].append(latency)

        annotated = results.plot()
        cv2.putText(annotated, f"F{frame_idx:04d} | persons:{persons} vehicles:{vehicles} | {latency:.0f}ms",
                    (8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)

        if writer:
            writer.write(annotated)

        if frame_idx % 25 == 0:
            log.info(f"Frame {frame_idx:>4d}/{total}: persons={persons} vehicles={vehicles} "
                     f"other={other}  latency={latency:.0f}ms")

        frame_idx += 1

    cap.release()
    if writer:
        writer.release()
        log.info(f"Annotated video saved → {out_path}")

    _print_summary(stats)


# ─── gRPC mode ───────────────────────────────────────────────────────────────

def run_grpc(video_path: str, addr: str, max_frames: int, out_path: str | None):
    import grpc
    from proto_gen import pipeline_pb2, pipeline_pb2_grpc

    GRPC_OPTS = [
        ("grpc.max_send_message_length", 100 * 1024 * 1024),
        ("grpc.max_receive_message_length", 100 * 1024 * 1024),
    ]

    channel = grpc.insecure_channel(addr, options=GRPC_OPTS)
    stub = pipeline_pb2_grpc.RouterServiceStub(channel)
    log.info(f"Connected to Router gRPC @ {addr}")

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        log.error(f"Cannot open video: {video_path}")
        sys.exit(1)

    fps = cap.get(cv2.CAP_PROP_FPS) or 12.5
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    log.info(f"Video: {w}x{h} @ {fps:.1f}fps  total={total} frames")

    writer = None
    if out_path:
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        writer = cv2.VideoWriter(out_path, fourcc, fps, (w, h))

    stats = {"frames": 0, "persons": 0, "vehicles": 0, "other": 0, "latency_ms": []}

    frame_idx = 0
    errors = 0
    while True:
        ret, frame_bgr = cap.read()
        if not ret or (max_frames and frame_idx >= max_frames):
            break

        jpeg_bytes = encode_jpeg(frame_bgr)
        frame_msg = pipeline_pb2.Frame(
            data=jpeg_bytes,
            width=w,
            height=h,
            frame_index=frame_idx,
        )
        req = pipeline_pb2.RouterRequest(frame=frame_msg)

        t0 = time.time()
        try:
            resp = stub.Detect(req, timeout=30)
        except grpc.RpcError as e:
            log.error(f"Frame {frame_idx}: gRPC error {e.code()}: {e.details()}")
            errors += 1
            frame_idx += 1
            continue
        rtt = (time.time() - t0) * 1000

        persons = (resp.pedestrian_result.person_count
                   if resp.HasField("pedestrian_result") else 0)
        vehicles = (resp.vehicle_result.current_total
                    if resp.HasField("vehicle_result") else 0)
        other = len(resp.unhandled)

        stats["frames"] += 1
        stats["persons"] += persons
        stats["vehicles"] += vehicles
        stats["other"] += other
        stats["latency_ms"].append(resp.total_latency_ms or rtt)

        if writer and resp.merged_frame:
            annotated = decode_jpeg(resp.merged_frame)
            cv2.putText(annotated,
                        f"F{frame_idx:04d} | persons:{persons} vehicles:{vehicles} | {resp.total_latency_ms:.0f}ms",
                        (8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
            writer.write(annotated)

        if frame_idx % 25 == 0:
            log.info(f"Frame {frame_idx:>4d}/{total}: persons={persons} vehicles={vehicles} "
                     f"other={other}  latency={resp.total_latency_ms:.0f}ms")

        frame_idx += 1

    cap.release()
    if writer:
        writer.release()
        log.info(f"Annotated video saved → {out_path}")

    if errors:
        log.warning(f"{errors} frames failed over gRPC")
    _print_summary(stats)


# ─── summary ─────────────────────────────────────────────────────────────────

def _print_summary(stats: dict):
    lat = stats["latency_ms"]
    print("\n" + "=" * 50)
    print("  VALIDATION SUMMARY")
    print("=" * 50)
    print(f"  Frames processed : {stats['frames']}")
    print(f"  Total persons    : {stats['persons']}")
    print(f"  Total vehicles   : {stats['vehicles']}")
    print(f"  Other detections : {stats['other']}")
    if lat:
        print(f"  Avg latency      : {sum(lat)/len(lat):.1f} ms")
        print(f"  Min / Max latency: {min(lat):.1f} / {max(lat):.1f} ms")
    print("=" * 50)


# ─── entry ───────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description="Validate traffic video through AI pipeline")
    p.add_argument("--video", default="datasets/traffic_car.mp4",
                   help="Input video path (default: datasets/traffic_car.mp4)")
    p.add_argument("--local", action="store_true",
                   help="Run YOLOv8 locally (no gRPC services required)")
    p.add_argument("--grpc", action="store_true",
                   help="Send frames to Router gRPC service")
    p.add_argument("--addr", default="localhost:50051",
                   help="Router gRPC address (default: localhost:50051)")
    p.add_argument("--max-frames", type=int, default=0,
                   help="Max frames to process (0 = all)")
    p.add_argument("--out", default=None,
                   help="Save annotated output video to this path")
    args = p.parse_args()

    if not args.local and not args.grpc:
        # default to local if neither flag given
        args.local = True

    if args.local:
        log.info("Mode: LOCAL (YOLOv8 direct)")
        run_local(args.video, args.max_frames, args.out)
    else:
        log.info("Mode: gRPC pipeline")
        run_grpc(args.video, args.addr, args.max_frames, args.out)


if __name__ == "__main__":
    main()
