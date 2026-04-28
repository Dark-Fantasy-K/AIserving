"""
Vehicle gRPC Service
────────────────────
接收车辆检测 → IoU 跟踪 → 计数统计 → 返回标注图
"""

import io
import time
import logging
from concurrent import futures
from collections import defaultdict
from typing import Dict, List

import cv2
import grpc
import numpy as np
from PIL import Image

from proto_gen import pipeline_pb2, pipeline_pb2_grpc

logging.basicConfig(level=logging.INFO, format="%(asctime)s [VEH] %(message)s")
logger = logging.getLogger(__name__)

CLASS_COLORS = {
    "car":        (251, 146, 60),
    "truck":      (96, 165, 250),
    "bus":        (250, 204, 21),
    "motorcycle": (192, 132, 252),
}
DEFAULT_COLOR = (200, 200, 200)
COLOR_TEXT_BG = (18, 18, 26)


def decode_frame(frame_msg):
    img = Image.open(io.BytesIO(frame_msg.data)).convert("RGB")
    return np.array(img)


def encode_frame(frame_np, quality=85):
    img = Image.fromarray(frame_np)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def _iou(a, b):
    x1 = max(a[0], b[0]); y1 = max(a[1], b[1])
    x2 = min(a[2], b[2]); y2 = min(a[3], b[3])
    inter = max(0, x2 - x1) * max(0, y2 - y1)
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0


class SimpleTracker:
    def __init__(self, iou_threshold=0.3, max_lost=30):
        self.iou_threshold = iou_threshold
        self.max_lost = max_lost
        self.tracks = {}
        self.next_id = 1
        self.total_count = defaultdict(int)

    def update(self, detections):
        updated = []
        used = set()

        sorted_dets = sorted(detections, key=lambda d: -d["confidence"])

        for det in sorted_dets:
            best_id, best_iou = None, 0
            for tid, trk in self.tracks.items():
                if tid in used:
                    continue
                iou = _iou(det["bbox"], trk["bbox"])
                if iou > best_iou:
                    best_iou = iou
                    best_id = tid

            if best_id and best_iou >= self.iou_threshold:
                self.tracks[best_id]["bbox"] = det["bbox"]
                self.tracks[best_id]["lost"] = 0
                det["track_id"] = best_id
                used.add(best_id)
            else:
                det["track_id"] = self.next_id
                self.tracks[self.next_id] = {
                    "bbox": det["bbox"], "class": det["class_name"], "lost": 0
                }
                self.total_count[det["class_name"]] += 1
                self.next_id += 1

            updated.append(det)

        for tid in list(self.tracks.keys()):
            if tid not in used:
                self.tracks[tid]["lost"] += 1
                if self.tracks[tid]["lost"] > self.max_lost:
                    del self.tracks[tid]

        return updated


class VehicleServicer(pipeline_pb2_grpc.VehicleServiceServicer):

    def __init__(self):
        self.tracker = SimpleTracker()
        logger.info("Vehicle tracker initialized")

    def ProcessFrame(self, request, context):
        start = time.time()
        frame = decode_frame(request.frame)

        # 解析检测
        dets = []
        for d in request.detections:
            dets.append({
                "class_name": d.class_name,
                "confidence": d.confidence,
                "bbox": [d.bbox.x1, d.bbox.y1, d.bbox.x2, d.bbox.y2],
            })

        tracked = self.tracker.update(dets)

        # 统计
        current_counts = defaultdict(int)
        vehicles = []
        for v in tracked:
            current_counts[v["class_name"]] += 1
            vehicles.append(pipeline_pb2.Vehicle(
                class_name=v["class_name"],
                confidence=v["confidence"],
                bbox=pipeline_pb2.BoundingBox(
                    x1=v["bbox"][0], y1=v["bbox"][1],
                    x2=v["bbox"][2], y2=v["bbox"][3],
                ),
                track_id=v["track_id"],
            ))

        # 标注
        annotated = frame.copy()
        for v in tracked:
            color = CLASS_COLORS.get(v["class_name"], DEFAULT_COLOR)
            x1, y1, x2, y2 = [int(c) for c in v["bbox"]]
            cv2.rectangle(annotated, (x1, y1), (x2, y2), color, 2)
            label = f'#{v["track_id"]} {v["class_name"]} {v["confidence"]:.0%}'
            (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
            cv2.rectangle(annotated, (x1, y1 - th - 8), (x1 + tw + 8, y1), COLOR_TEXT_BG, -1)
            cv2.putText(annotated, label, (x1 + 4, y1 - 4),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1, cv2.LINE_AA)

        # 计数面板
        panel_lines = [f"Vehicles: {sum(current_counts.values())}"]
        for cls, cnt in sorted(current_counts.items()):
            panel_lines.append(f"  {cls}: {cnt}")
        panel_lines.append(f"Tracks: {len(self.tracker.tracks)}")
        y_off = 30
        for line in panel_lines:
            (tw, th), _ = cv2.getTextSize(line, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 1)
            cv2.rectangle(annotated, (10, y_off - th - 4), (20 + tw, y_off + 4), COLOR_TEXT_BG, -1)
            cv2.putText(annotated, line, (14, y_off),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (251, 146, 60), 1, cv2.LINE_AA)
            y_off += th + 12

        latency = round((time.time() - start) * 1000, 1)
        logger.info(f"Tracked {len(vehicles)} vehicles in {latency}ms")

        return pipeline_pb2.VehicleResponse(
            current_total=sum(current_counts.values()),
            active_tracks=len(self.tracker.tracks),
            vehicles=vehicles,
            current_counts=[
                pipeline_pb2.VehicleCount(class_name=k, count=v)
                for k, v in current_counts.items()
            ],
            cumulative=[
                pipeline_pb2.VehicleCount(class_name=k, count=v)
                for k, v in self.tracker.total_count.items()
            ],
            annotated_frame=encode_frame(annotated),
            latency_ms=latency,
        )


def serve():
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=4),
        options=[
            ("grpc.max_send_message_length", 100 * 1024 * 1024),
            ("grpc.max_receive_message_length", 100 * 1024 * 1024),
        ],
    )
    pipeline_pb2_grpc.add_VehicleServiceServicer_to_server(
        VehicleServicer(), server
    )
    server.add_insecure_port("[::]:50053")
    logger.info("Vehicle service listening on :50053")
    server.start()
    server.wait_for_termination()


if __name__ == "__main__":
    serve()
