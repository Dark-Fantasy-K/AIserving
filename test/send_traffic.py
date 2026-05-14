#!/usr/bin/env python3
"""
Send video frames to the gateway /predict endpoint to generate load.
Supports multiple concurrent streams (multi-stream input/output).

Usage:
  python3 test/send_traffic.py
  python3 test/send_traffic.py --video samples/burst_traffic.mp4 --fps 2 --loops 3
  python3 test/send_traffic.py --video datasets/persons.mp4 datasets/vehicle_persons.mp4 --fps 2
  python3 test/send_traffic.py --video datasets/persons.mp4 --streams 4 --fps 24
"""
import argparse
import io
import time
import sys
import threading
import cv2
import requests
from pathlib import Path
from PIL import Image

#GATEWAY = "http://localhost:5000"
GATEWAY = "http://192.168.178.101:32525"

def extract_frames(video_path: str, fps: float) -> list[bytes]:
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        print(f"Cannot open video: {video_path}")
        sys.exit(1)

    native_fps = cap.get(cv2.CAP_PROP_FPS) or 25
    step = max(1, int(native_fps / fps))
    frames = []
    idx = 0
    while True:
        ret, frame = cap.read()
        if not ret:
            break
        if idx % step == 0:
            img = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            img.thumbnail((640, 640), Image.BILINEAR)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=85)
            frames.append(buf.getvalue())
        idx += 1
    cap.release()
    avg_kb = sum(len(f) for f in frames) / len(frames) / 1024 if frames else 0
    print(f"  [{Path(video_path).name}] Extracted {len(frames)} frames "
          f"(source {native_fps:.0f}fps → {fps}fps, avg {avg_kb:.1f}KB/frame)")
    return frames


def send_stream(stream_id: int, video_path: str, frames: list[bytes],
                interval: float, loops: int, mode: str, slim: bool, results: dict):
    total = ok = err = 0
    latencies = []
    session = requests.Session()
    params = {"mode": mode}
    if slim:
        params["slim"] = "1"
    headers = {"Content-Type": "image/jpeg"}
    label = f"S{stream_id}[{Path(video_path).name}]"

    for loop in range(loops):
        print(f"\n── {label} Loop {loop+1}/{loops} ──")
        for i, frame in enumerate(frames):
            t0 = time.time()
            try:
                r = session.post(f"{GATEWAY}/predict", data=frame,
                                 params=params, headers=headers, timeout=30)
                rtt = (time.time() - t0) * 1000
                total += 1
                if r.status_code == 200:
                    ok += 1
                    body = r.json()
                    latency = body.get("processing_ms", rtt)
                    latencies.append(latency)
                    det = body.get("total_detections", 0)
                    ped_data = body.get("PersonPoseHandler", {})
                    persons  = ped_data.get("person_count", 0)
                    vehicles = body.get("VehicleCountHandler", {}).get("current_total", 0)
                    ped_ids  = [str(p.get("pedestrian_id", "?"))
                                for p in ped_data.get("persons", [])]
                    id_str = " ids=[" + ",".join(ped_ids) + "]" if ped_ids else ""
                    print(f"  {label} [{i+1:3d}] {latency:6.1f}ms "
                          f"(rtt {rtt:.0f}ms)  det={det} persons={persons} vehicles={vehicles}{id_str}")
                else:
                    err += 1
                    print(f"  {label} [{i+1:3d}] HTTP {r.status_code}: {r.text[:80]}")
            except Exception as e:
                err += 1
                total += 1
                print(f"  {label} [{i+1:3d}] Request failed: {e}")
            time.sleep(interval)

    results[stream_id] = {"total": total, "ok": ok, "err": err,
                          "latencies": latencies, "label": label}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--video",    nargs="+", default=["samples/burst_traffic.mp4"],
                   help="one or more video files; each becomes an independent concurrent stream")
    p.add_argument("--streams",  type=int, default=None,
                   help="total concurrent streams (videos repeated round-robin if fewer videos than streams)")
    p.add_argument("--fps",      type=float, default=1.0,  help="frames to sample per second")
    p.add_argument("--loops",    type=int,   default=2,    help="number of full-video passes per stream")
    p.add_argument("--interval", type=float, default=0.5,  help="seconds between requests within a stream")
    p.add_argument("--mode",     default="both", choices=["pedestrian", "vehicle", "both"])
    p.add_argument("--slim",     action="store_true", help="skip annotated image in response")
    args = p.parse_args()

    videos = []
    for v in args.video:
        path = Path(v)
        if not path.exists():
            print(f"Video not found: {path}")
            sys.exit(1)
        videos.append(str(path))

    # Build per-stream video assignments
    if args.streams:
        stream_videos = [videos[i % len(videos)] for i in range(args.streams)]
    else:
        stream_videos = videos

    print(f"Input videos : {[Path(v).name for v in videos]}")
    print(f"Output streams: {len(stream_videos)}")
    print(f"Mode: {args.mode}  fps: {args.fps}  loops: {args.loops}  interval: {args.interval}s")
    print(f"Gateway: {GATEWAY}")

    try:
        requests.get(f"{GATEWAY}/health", timeout=5)
    except Exception:
        print("\n[Error] Gateway not reachable. Start services first: docker compose up -d")
        sys.exit(1)

    # Extract frames once per unique video path
    print("\nExtracting frames...")
    frame_cache: dict[str, list[bytes]] = {}
    for v in dict.fromkeys(stream_videos):  # preserves order, deduplicates
        frame_cache[v] = extract_frames(v, args.fps)

    # Launch one thread per stream
    results: dict[int, dict] = {}
    threads = [
        threading.Thread(
            target=send_stream,
            args=(sid, vpath, frame_cache[vpath],
                  args.interval, args.loops, args.mode, args.slim, results),
            daemon=True,
        )
        for sid, vpath in enumerate(stream_videos)
    ]

    print(f"\nStarting {len(threads)} concurrent stream(s)...\n")
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Per-stream and aggregate summary
    print(f"\n{'='*55}")
    print(f"{'Per-stream summary':^55}")
    print(f"{'='*55}")
    all_latencies: list[float] = []
    total_ok = total_err = 0
    for sid in sorted(results):
        r = results[sid]
        avg = sum(r["latencies"]) / len(r["latencies"]) if r["latencies"] else 0
        print(f"  {r['label']:35s}  OK={r['ok']:4d}  Err={r['err']:3d}  avg={avg:.0f}ms")
        all_latencies.extend(r["latencies"])
        total_ok += r["ok"]
        total_err += r["err"]

    print(f"{'─'*55}")
    print(f"  Total: OK={total_ok}  Errors={total_err}  streams={len(results)}")
    if all_latencies:
        avg = sum(all_latencies) / len(all_latencies)
        print(f"  Overall latency: avg={avg:.0f}ms  min={min(all_latencies):.0f}ms  max={max(all_latencies):.0f}ms")
    print(f"{'='*55}")
    print(f"\nView traces: http://localhost:16686  (Jaeger UI)")


if __name__ == "__main__":
    main()
