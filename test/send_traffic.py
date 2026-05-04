#!/usr/bin/env python3
"""
Send video frames to the gateway /predict endpoint to generate load.

Usage:
  python test/send_traffic.py
  python test/send_traffic.py --video samples/traffic_car.mp4 --fps 2 --loops 3
"""
import argparse
import io
import time
import sys
import cv2
import requests
from pathlib import Path

GATEWAY = "http://localhost:5000"


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
            _, buf = cv2.imencode(".jpg", frame)
            frames.append(buf.tobytes())
        idx += 1
    cap.release()
    print(f"  Extracted {len(frames)} frames (source {native_fps:.0f}fps → {fps}fps)")
    return frames


def send(frames: list[bytes], interval: float, loops: int, mode: str):
    total = ok = err = 0
    latencies = []

    for loop in range(loops):
        print(f"\n── Loop {loop+1}/{loops} ──")
        for i, frame in enumerate(frames):
            files = {"image": ("frame.jpg", io.BytesIO(frame), "image/jpeg")}
            data = {"mode": mode}
            t0 = time.time()
            try:
                r = requests.post(f"{GATEWAY}/predict", files=files, data=data, timeout=30)
                latency = (time.time() - t0) * 1000
                latencies.append(latency)
                total += 1
                if r.status_code == 200:
                    ok += 1
                    body = r.json()
                    total = body.get("total_detections", 0)
                    persons = body.get("PersonPoseHandler", {}).get("person_count", 0)
                    vehicles = body.get("VehicleCountHandler", {}).get("current_total", 0)
                    print(f"  [{i+1:3d}] {latency:6.0f}ms  total={total} persons={persons} vehicles={vehicles}")
                else:
                    err += 1
                    print(f"  [{i+1:3d}] HTTP {r.status_code}: {r.text[:80]}")
            except Exception as e:
                err += 1
                total += 1
                print(f"  [{i+1:3d}] Request failed: {e}")
            time.sleep(interval)

    print(f"\n{'='*40}")
    print(f"Total: {total}  OK: {ok}  Errors: {err}")
    if latencies:
        avg = sum(latencies) / len(latencies)
        print(f"Latency: avg={avg:.0f}ms  min={min(latencies):.0f}ms  max={max(latencies):.0f}ms")
    print(f"{'='*40}")
    print(f"\nView traces: http://localhost:16686  (Jaeger UI)")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--video",    default="samples/person_vehicle.mp4")
    p.add_argument("--fps",      type=float, default=1.0,  help="frames to sample per second")
    p.add_argument("--loops",    type=int,   default=2,    help="number of full-video passes")
    p.add_argument("--interval", type=float, default=0.5,  help="seconds between requests")
    p.add_argument("--mode",     default="both", choices=["pedestrian", "vehicle", "both"])
    args = p.parse_args()

    video = Path(args.video)
    if not video.exists():
        print(f"Video not found: {video}")
        sys.exit(1)

    print(f"Video: {video}")
    print(f"Mode: {args.mode}  fps: {args.fps}  loops: {args.loops}  interval: {args.interval}s")
    print(f"Gateway: {GATEWAY}")

    try:
        requests.get(f"{GATEWAY}/health", timeout=5)
    except Exception:
        print(f"\n[Error] Gateway not reachable. Start services first: docker compose up -d")
        sys.exit(1)

    frames = extract_frames(str(video), args.fps)
    send(frames, args.interval, args.loops, args.mode)


if __name__ == "__main__":
    main()
