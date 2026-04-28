"""
make_burst_dataset.py
─────────────────────
生成突发流量测试视频：
  - 0~2s   : 空白画面（0 人）
  - 2~7s   : 人群突然涌入（每帧随机散布 N 个人物 crop）
  - 7~10s  : 持续拥挤（同上，保持高密度）

用法:
  python make_burst_dataset.py \
      --source  datasets/person_vehicle.mp4 \
      --out     datasets/burst_traffic.mp4  \
      --persons 15          # 每帧最多人数
      --fps     12
"""

import argparse
import random
import sys
import cv2
import numpy as np
from ultralytics import YOLO


# ── 从视频里提取清晰的人物 crop ──────────────────────────────────────

def collect_person_crops(video_path: str, model: YOLO,
                         min_h: int = 60, max_crops: int = 40) -> list:
    cap = cv2.VideoCapture(video_path)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    crops = []
    step = max(1, total // 120)          # 最多采样 120 帧

    for i in range(0, total, step):
        cap.set(cv2.CAP_PROP_POS_FRAMES, i)
        ret, frame = cap.read()
        if not ret:
            continue
        results = model(frame, verbose=False)[0]
        for box in results.boxes:
            if results.names[int(box.cls)] != "person":
                continue
            x1, y1, x2, y2 = map(int, box.xyxy[0].tolist())
            h, w = y2 - y1, x2 - x1
            if h < min_h or w < 10:
                continue
            crop = frame[y1:y2, x1:x2].copy()
            crops.append(crop)
            if len(crops) >= max_crops:
                break
        if len(crops) >= max_crops:
            break

    cap.release()
    return crops


# ── 把若干 crop 随机贴到背景帧上 ──────────────────────────────────────

def paste_crowd(background: np.ndarray, crops: list,
                n_persons: int, rng: random.Random) -> np.ndarray:
    frame = background.copy()
    H, W = frame.shape[:2]
    placed = rng.sample(crops, min(n_persons, len(crops)))

    for crop in placed:
        ch, cw = crop.shape[:2]
        # 随机缩放 0.6~1.2 倍
        scale = rng.uniform(0.6, 1.2)
        nh, nw = max(1, int(ch * scale)), max(1, int(cw * scale))
        resized = cv2.resize(crop, (nw, nh))

        # 随机位置
        x = rng.randint(0, max(0, W - nw - 1))
        y = rng.randint(max(0, H - nh * 2), max(0, H - nh))   # 偏下方（地面区域）

        # 简单 alpha 合并（按亮度做软边缘）
        roi = frame[y:y + nh, x:x + nw]
        if roi.shape[:2] != (nh, nw):
            continue
        mask = cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY)
        _, mask = cv2.threshold(mask, 15, 255, cv2.THRESH_BINARY)
        mask_3 = cv2.merge([mask, mask, mask]).astype(np.float32) / 255.0
        blended = (resized.astype(np.float32) * mask_3 +
                   roi.astype(np.float32) * (1 - mask_3)).astype(np.uint8)
        frame[y:y + nh, x:x + nw] = blended

    return frame


# ── 构造一帧空白背景（灰色街道感） ────────────────────────────────────

def make_background(w: int, h: int) -> np.ndarray:
    bg = np.full((h, w, 3), 80, dtype=np.uint8)   # 深灰
    # 简单地面线条
    cv2.rectangle(bg, (0, h * 2 // 3), (w, h), (60, 60, 60), -1)
    cv2.line(bg, (w // 2, 0), (w // 2, h), (70, 70, 70), 1)
    return bg


# ── 主流程 ────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source",  default="datasets/person_vehicle.mp4")
    ap.add_argument("--out",     default="datasets/burst_traffic.mp4")
    ap.add_argument("--persons", type=int, default=15, help="crowd 阶段每帧人数上限")
    ap.add_argument("--fps",     type=int, default=12)
    ap.add_argument("--width",   type=int, default=768)
    ap.add_argument("--height",  type=int, default=432)
    ap.add_argument("--seed",    type=int, default=42)
    args = ap.parse_args()

    rng  = random.Random(args.seed)
    W, H = args.width, args.height
    FPS  = args.fps

    # 时间段（秒 → 帧数）
    T_BLANK  = 2        # 空白段
    T_BURST  = 5        # 突发拥挤段
    T_DENSE  = 3        # 持续拥挤段
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
    print()

    # ── 1) 提取人物 crop ──────────────────────────────────
    print(f"Loading YOLOv8s & extracting person crops from {args.source} ...")
    model  = YOLO("yolov8s.pt")
    crops  = collect_person_crops(args.source, model)
    if not crops:
        print("ERROR: no person crops found in source video.")
        sys.exit(1)
    print(f"  Collected {len(crops)} person crops.")

    # ── 2) 初始化 VideoWriter ─────────────────────────────
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

    # ── 3) 空白段（0~2s） ──────────────────────────────────
    print(f"Generating blank segment ({N_BLANK} frames)...")
    for _ in range(N_BLANK):
        write_frame(bg.copy(), "IDLE  persons=0")

    # ── 4) 突发段（2~7s）：人数从 1 快速线性增加到 max ───────
    print(f"Generating burst segment ({N_BURST} frames)...")
    for i in range(N_BURST):
        # 0 → args.persons 线性增长（前半段）
        n = max(1, int((i + 1) / N_BURST * args.persons))
        frame = paste_crowd(bg, crops, n, rng)
        write_frame(frame, f"BURST persons={n}")

    # ── 5) 持续拥挤段（7~10s） ────────────────────────────
    print(f"Generating dense segment ({N_DENSE} frames)...")
    for _ in range(N_DENSE):
        n = args.persons + rng.randint(-2, 2)
        frame = paste_crowd(bg, crops, n, rng)
        write_frame(frame, f"DENSE persons≈{args.persons}")

    writer.release()
    print()
    print(f"Done → {args.out}  ({frame_idx} frames total)")
    print("=" * 50)


if __name__ == "__main__":
    main()
