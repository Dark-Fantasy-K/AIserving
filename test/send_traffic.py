#!/usr/bin/env python3
"""
Send video frames to the gateway /predict endpoint to generate load.

Usage:
  python3 test/send_traffic.py
  python3 test/send_traffic.py --video datasets/persons.mp4 --fps 2 --loops 3 --rps 1
  python3 test/send_traffic.py --video samples/simple_test.mp4 --fps 1 --loops 5 --rps 1 
"""
import argparse
import asyncio
import io
import time
import sys
from pathlib import Path

import aiohttp
import cv2
GATEWAY = "http://localhost:5000"
#GATEWAY = "http://172.18.0.4:30398"

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


async def send_frame(
    session: aiohttp.ClientSession,
    frame: bytes,
    idx: int,
    mode: str,
    latencies: list[float],
    counters: dict,
    sem: asyncio.Semaphore,
):
    async with sem:                          
        form = aiohttp.FormData()
        form.add_field("image", io.BytesIO(frame), filename="frame.jpg", content_type="image/jpeg")
        form.add_field("mode", mode)

        t0 = time.time()
        try:
            async with session.post(
                f"{GATEWAY}/predict",
                data=form,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as r:
                latency = (time.time() - t0) * 1000
                latencies.append(latency)
                counters["total"] += 1

                if r.status == 200:
                    counters["ok"] += 1
                    body = await r.json()
                    total_det = body.get("total_detections", 0)
                    persons   = body.get("PersonPoseHandler", {}).get("person_count", 0)
                    vehicles  = body.get("VehicleCountHandler", {}).get("current_total", 0)
                    print(
                        f"  [{idx+1:4d}] {latency:6.0f}ms  "
                        f"total={total_det} persons={persons} vehicles={vehicles}"
                    )
                else:
                    counters["err"] += 1
                    text = await r.text()
                    print(f"  [{idx+1:4d}] HTTP {r.status}: {text[:80]}")

        except asyncio.TimeoutError:
            counters["err"] += 1
            counters["total"] += 1
            print(f"  [{idx+1:4d}] Timeout")
        except Exception as e:
            counters["err"] += 1
            counters["total"] += 1
            print(f"  [{idx+1:4d}] Error: {e}")


async def send_async(
    frames: list[bytes],
    loops: int,
    mode: str,
    rps: float,
    max_concurrency: int,
):
    interval = 1.0 / rps          
    sem = asyncio.Semaphore(max_concurrency)
    latencies: list[float] = []
    counters = {"total": 0, "ok": 0, "err": 0}

    async with aiohttp.ClientSession() as session:
        tasks = []
        global_idx = 0

        for loop in range(loops):
            print(f"\n── Loop {loop+1}/{loops} ──")
            for frame in frames:
                task = asyncio.create_task(
                    send_frame(session, frame, global_idx, mode, latencies, counters, sem)
                )
                tasks.append(task)
                global_idx += 1
                await asyncio.sleep(interval)   

        await asyncio.gather(*tasks, return_exceptions=True)

    _print_summary(counters, latencies)


def _print_summary(counters: dict, latencies: list[float]):
    print(f"\n{'='*45}")
    print(f"Total: {counters['total']}  OK: {counters['ok']}  Errors: {counters['err']}")
    if latencies:
        avg = sum(latencies) / len(latencies)
        p95 = sorted(latencies)[int(len(latencies) * 0.95)]
        print(
            f"Latency: avg={avg:.0f}ms  "
            f"min={min(latencies):.0f}ms  "
            f"p95={p95:.0f}ms  "
            f"max={max(latencies):.0f}ms"
        )
    print(f"{'='*45}")
    print(f"\nView traces: http://localhost:16686  (Jaeger UI)")


async def check_gateway():
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{GATEWAY}/health", timeout=aiohttp.ClientTimeout(total=5)):
                pass
    except Exception:
        print("[Error] Gateway not reachable. Start services first: docker compose up -d")
        sys.exit(1)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--video",           default="samples/person_vehicle.mp4")
    p.add_argument("--fps",             type=float, default=1.0,   help="frames to sample per second")
    p.add_argument("--loops",           type=int,   default=2,     help="number of full-video passes")
    p.add_argument("--rps",             type=float, default=2.0,   help="requests per second to send")
    p.add_argument("--max-concurrency", type=int,   default=10,    help="max in-flight requests")
    p.add_argument("--mode",            default="both", choices=["pedestrian", "vehicle", "both"])
    args = p.parse_args()

    video = Path(args.video)
    if not video.exists():
        print(f"Video not found: {video}")
        sys.exit(1)

    print(f"Video   : {video}")
    print(f"Mode    : {args.mode}  fps={args.fps}  loops={args.loops}")
    print(f"RPS     : {args.rps}  max-concurrency={args.max_concurrency}")
    print(f"Gateway : {GATEWAY}")

    asyncio.run(check_gateway())
    frames = extract_frames(str(video), args.fps)
    asyncio.run(send_async(frames, args.loops, args.mode, args.rps, args.max_concurrency))


if __name__ == "__main__":
    main()