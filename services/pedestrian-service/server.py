"""
Pedestrian gRPC Service
───────────────────────
接收 person 检测 → YOLOv8s-pose 姿态估计 → 返回关键点 + 标注图
"""

import io
import time
import logging
from concurrent import futures

import cv2
import grpc
import numpy as np
from PIL import Image
from ultralytics import YOLO

from proto_gen import pipeline_pb2, pipeline_pb2_grpc

logging.basicConfig(level=logging.INFO, format="%(asctime)s [PED] %(message)s")
logger = logging.getLogger(__name__)

KEYPOINT_NAMES = [
    "nose", "left_eye", "right_eye", "left_ear", "right_ear",
    "left_shoulder", "right_shoulder", "left_elbow", "right_elbow",
    "left_wrist", "right_wrist", "left_hip", "right_hip",
    "left_knee", "right_knee", "left_ankle", "right_ankle",
]

SKELETON = [
    (0, 1), (0, 2), (1, 3), (2, 4),
    (5, 6), (5, 7), (7, 9), (6, 8), (8, 10),
    (5, 11), (6, 12), (11, 12),
    (11, 13), (13, 15), (12, 14), (14, 16),
]

COLOR_KP = (110, 231, 183)
COLOR_SKEL = (80, 180, 140)
COLOR_BBOX = (110, 231, 183)
COLOR_TEXT_BG = (18, 18, 26)


def decode_frame(frame_msg):
    """proto Frame → numpy RGB"""
    img = Image.open(io.BytesIO(frame_msg.data)).convert("RGB")
    return np.array(img)


def encode_frame(frame_np, quality=85):
    """numpy RGB → JPEG bytes"""
    img = Image.fromarray(frame_np)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


class PedestrianServicer(pipeline_pb2_grpc.PedestrianServiceServicer):

    def __init__(self):
        logger.info("Loading YOLOv8s-pose...")
        t0 = time.time()
        self.pose_model = YOLO("yolov8s-pose.pt")
        logger.info(f"YOLOv8s-pose loaded in {time.time() - t0:.2f}s")

    def ProcessFrame(self, request, context):
        start = time.time()

        frame = decode_frame(request.frame)
        results = self.pose_model(frame, verbose=False)[0]

        persons = []
        annotated = frame.copy()

        if results.keypoints is not None:
            kpts_data = results.keypoints.data.cpu().numpy()
            boxes = results.boxes

            for i in range(len(kpts_data)):
                kpts = kpts_data[i]
                bbox = boxes.xyxy[i].tolist()
                conf = float(boxes.conf[i])

                # proto keypoints
                proto_kps = []
                pts = []
                for j, name in enumerate(KEYPOINT_NAMES):
                    x, y, c = float(kpts[j][0]), float(kpts[j][1]), float(kpts[j][2])
                    proto_kps.append(pipeline_pb2.Keypoint(
                        name=name, x=round(x, 1), y=round(y, 1), confidence=round(c, 3)
                    ))
                    pts.append((int(x), int(y), c))

                persons.append(pipeline_pb2.PersonPose(
                    bbox=pipeline_pb2.BoundingBox(
                        x1=bbox[0], y1=bbox[1], x2=bbox[2], y2=bbox[3]
                    ),
                    confidence=round(conf, 4),
                    keypoints=proto_kps,
                ))

                # annotate
                x1, y1, x2, y2 = [int(v) for v in bbox]
                cv2.rectangle(annotated, (x1, y1), (x2, y2), COLOR_BBOX, 2)
                label = f'person {conf:.0%}'
                (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
                cv2.rectangle(annotated, (x1, y1 - th - 8), (x1 + tw + 8, y1), COLOR_TEXT_BG, -1)
                cv2.putText(annotated, label, (x1 + 4, y1 - 4),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, COLOR_BBOX, 1, cv2.LINE_AA)

                for a, b in SKELETON:
                    if pts[a][2] > 0.3 and pts[b][2] > 0.3:
                        cv2.line(annotated, (pts[a][0], pts[a][1]),
                                 (pts[b][0], pts[b][1]), COLOR_SKEL, 2, cv2.LINE_AA)
                for px, py, pc in pts:
                    if pc > 0.3:
                        cv2.circle(annotated, (px, py), 4, COLOR_KP, -1, cv2.LINE_AA)

        latency = round((time.time() - start) * 1000, 1)
        logger.info(f"Processed {len(persons)} persons in {latency}ms")

        return pipeline_pb2.PedestrianResponse(
            person_count=len(persons),
            persons=persons,
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
    pipeline_pb2_grpc.add_PedestrianServiceServicer_to_server(
        PedestrianServicer(), server
    )
    server.add_insecure_port("[::]:50052")
    logger.info("Pedestrian service listening on :50052")
    server.start()
    server.wait_for_termination()


if __name__ == "__main__":
    serve()
