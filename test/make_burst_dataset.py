"""
make_burst_dataset.py
─────────────────────
Generate a synthetic burst-traffic test video:
  - 0~2s  : blank scene (0 persons, 0 vehicles)
  - 2~7s  : sudden crowd surge (persons + vehicles ramp up linearly)
  - 7~10s : sustained dense traffic (held at peak density)

Usage:
  python make_burst_dataset.py \
      --source  datasets/person_vehicle.mp4 \
      --out     datasets/burst_traffic.mp4  \
      --persons 15          # max persons per crowd frame
      --vehicles 8          # max vehicles per crowd frame
      --fps     12
"""

import argparse
import random
import sys
import cv2
import numpy as np
from ultralytics import YOLO

VEHICLE_CLASSES = {"car", "truck", "bus", "motorcycle"}


# ── Extract crops of given classes from the source video ─────────────

def collect_crops(video_path: str, model: YOLO, target_classes: set,
                  min_h: int = 40, min_w: int = 40, max_crops: int = 40) -> list:
    cap = cv2.VideoCapture(video_path)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    crops = []
    step = max(1, total // 120)   # sample at most 120 frames

    for i in range(0, total, step):
        cap.set(cv2.CAP_PROP_POS_FRAMES, i)
        ret, frame = cap.read()
        if not ret:
            continue
        results = model(frame, verbose=False)[0]
        for box in results.boxes:
            cls_name = results.names[int(box.cls)]
            if cls_name not in target_classes:
                continue
            x1, y1, x2, y2 = map(int, box.xyxy[0].tolist())
            if (y2 - y1) < min_h or (x2 - x1) < min_w:
                continue
            crops.append(frame[y1:y2, x1:x2].copy())
            if len(crops) >= max_crops:
                break
        if len(crops) >= max_crops:
            break

    cap.release()
    return crops


# ── Blend a single crop onto the frame ───────────────────────────────

def _paste(frame: np.ndarray, crop: np.ndarray, x: int, y: int) -> None:
    ch, cw = crop.shape[:2]
    roi = frame[y:y + ch, x:x + cw]
    if roi.shape[:2] != (ch, cw):
        return
    mask = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    _, mask = cv2.threshold(mask, 15, 255, cv2.THRESH_BINARY)
    mask_3 = cv2.merge([mask, mask, mask]).astype(np.float32) / 255.0
    blended = (crop.astype(np.float32) * mask_3 +
               roi.astype(np.float32) * (1 - mask_3)).astype(np.uint8)
    frame[y:y + ch, x:x + cw] = blended


# ── Paste persons (bottom third) and vehicles (middle road band) ──────

def paste_scene(background: np.ndarray,
                person_crops: list, n_persons: int,
                vehicle_crops: list, n_vehicles: int,
                rng: random.Random) -> np.ndarray:
    frame = background.copy()
    H, W = frame.shape[:2]

    # vehicles occupy the middle road band (1/3 ~ 2/3 of height)
    if vehicle_crops and n_vehicles > 0:
        for crop in rng.sample(vehicle_crops, min(n_vehicles, len(vehicle_crops))):
            ch, cw = crop.shape[:2]
            scale = rng.uniform(0.7, 1.3)
            nh, nw = max(1, int(ch * scale)), max(1, int(cw * scale))
            resized = cv2.resize(crop, (nw, nh))
            x = rng.randint(0, max(0, W - nw - 1))
            y_min = H // 3
            y_max = max(y_min, 2 * H // 3 - nh)
            y = rng.randint(y_min, y_max) if y_max > y_min else y_min
            _paste(frame, resized, x, y)

    # persons occupy the bottom third
    if person_crops and n_persons > 0:
        for crop in rng.sample(person_crops, min(n_persons, len(person_crops))):
            ch, cw = crop.shape[:2]
            scale = rng.uniform(0.6, 1.2)
            nh, nw = max(1, int(ch * scale)), max(1, int(cw * scale))
            resized = cv2.resize(crop, (nw, nh))
            x = rng.randint(0, max(0, W - nw - 1))
            y_min = max(0, H - nh * 2)
            y_max = max(y_min, H - nh)
            y = rng.randint(y_min, y_max) if y_max > y_min else y_min
            _paste(frame, resized, x, y)

    return frame


# ── Build a blank background frame (dark-grey street look) ───────────

def make_background(w: int, h: int) -> np.ndarray:
    bg = np.full((h, w, 3), 80, dtype=np.uint8)   # dark grey
    # simple ground lines
    cv2.rectangle(bg, (0, h * 2 // 3), (w, h), (60, 60, 60), -1)
    cv2.line(bg, (w // 2, 0), (w // 2, h), (70, 70, 70), 1)
    return bg


# ── Main ─────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source",   default="samples/person_vehicle.mp4")
    ap.add_argument("--out",      default="samples/burst_traffic.mp4")
    ap.add_argument("--persons",  type=int, default=15, help="max persons per crowd frame")
    ap.add_argument("--vehicles", type=int, default=8,  help="max vehicles per crowd frame")
    ap.add_argument("--fps",      type=int, default=12)
    ap.add_argument("--width",    type=int, default=768)
    ap.add_argument("--height",   type=int, default=432)
    ap.add_argument("--seed",     type=int, default=42)
    args = ap.parse_args()

    rng  = random.Random(args.seed)
    W, H = args.width, args.height
    FPS  = args.fps

    # segment lengths in frames
    T_BLANK  = 2        # idle segment
    T_BURST  = 5        # burst segment
    T_DENSE  = 3        # sustained dense segment
    TOTAL_S  = T_BLANK + T_BURST + T_DENSE

    N_BLANK  = int(T_BLANK * FPS)
    N_BURST  = int(T_BURST * FPS)
    N_DENSE  = int(T_DENSE * FPS)

    print("=" * 50)
    print(f"  Burst Traffic Dataset Generator")
    print("=" * 50)
    print(f"  Output   : {args.out}")
    print(f"  Size     : {W}x{H} @ {FPS}fps  total={TOTAL_S}s")
    print(f"  Blank    : 0~{T_BLANK}s  ({N_BLANK} frames)")
    print(f"  Burst    : {T_BLANK}~{T_BLANK+T_BURST}s  ({N_BURST} frames)")
    print(f"  Dense    : {T_BLANK+T_BURST}~{TOTAL_S}s  ({N_DENSE} frames)")
    print(f"  Persons  : up to {args.persons} per crowd frame")
    print(f"  Vehicles : up to {args.vehicles} per crowd frame")
    print()

    # ── 1) extract crops ─────────────────────────────────────────────
    model = YOLO("yolov8s.pt")

    print(f"Extracting person crops from {args.source} ...")
    person_crops = collect_crops(args.source, model, {"person"}, min_h=60)
    print(f"  Collected {len(person_crops)} person crops.")

    vehicle_source = args.vehicle_source or args.source
    print(f"Extracting vehicle crops from {vehicle_source} ...")
    vehicle_crops = collect_crops(vehicle_source, model, VEHICLE_CLASSES, min_h=40)
    print(f"  Collected {len(vehicle_crops)} vehicle crops.")

    if not person_crops and not vehicle_crops:
        print("ERROR: no crops found in source video.")
        sys.exit(1)

    # ── 2) initialise VideoWriter ─────────────────────────────────────
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(args.out, fourcc, FPS, (W, H))
    bg     = make_background(W, H)

    frame_idx = 0

    def write_frame(img, label=""):
        nonlocal frame_idx
        out_frame = img.copy()
        ts = frame_idx / FPS
        cv2.putText(out_frame, f"t={ts:.1f}s  {label}",
                    (8, 26), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 220, 0), 2)
        writer.write(out_frame)
        frame_idx += 1

    # ── 3) idle segment (0~2s) ────────────────────────────────────────
    print(f"Generating blank segment ({N_BLANK} frames)...")
    for _ in range(N_BLANK):
        write_frame(bg.copy(), "IDLE  p=0 v=0")

    # ── 4) burst segment (2~7s): linearly ramp from 1 to max ─────────
    print(f"Generating burst segment ({N_BURST} frames)...")
    for i in range(N_BURST):
        ratio = (i + 1) / N_BURST
        np_ = max(1, int(ratio * args.persons))
        nv  = max(1, int(ratio * args.vehicles))
        frame = paste_scene(bg, person_crops, np_, vehicle_crops, nv, rng)
        write_frame(frame, f"BURST p={np_} v={nv}")

    # ── 5) dense segment (7~10s) ──────────────────────────────────────
    print(f"Generating dense segment ({N_DENSE} frames)...")
    for _ in range(N_DENSE):
        np_ = args.persons + rng.randint(-2, 2)
        nv  = args.vehicles + rng.randint(-1, 1)
        frame = paste_scene(bg, person_crops, np_, vehicle_crops, nv, rng)
        write_frame(frame, f"DENSE p≈{args.persons} v≈{args.vehicles}")

    writer.release()
    print()
    print(f"Done → {args.out}  ({frame_idx} frames total)")
    print("=" * 50)


if __name__ == "__main__":
    main()
