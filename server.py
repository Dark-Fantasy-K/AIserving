"""
Router gRPC Service
───────────────────
1. YOLOv8s 检测全图
2. 按类别分流: person → Pedestrian Service, 车辆 → Vehicle Service
3. 并行调用两个下游服务
4. 合并标注图 + 结果返回
"""

import io
import os
import time
import logging
from concurrent import futures

import cv2
import grpc
import numpy as np
from PIL import Image
from ultralytics import YOLO

from proto_gen import pipeline_pb2, pipeline_pb2_grpc

logging.basicConfig(level=logging.INFO, format="%(asctime)s [RTR] %(message)s")
logger = logging.getLogger(__name__)

VEHICLE_CLASSES = {"car", "truck", "bus", "motorcycle"}

# 下游服务地址（K8s 中用 Service DNS）
PED_ADDR = os.environ.get("PEDESTRIAN_SERVICE_ADDR", "localhost:50052")
VEH_ADDR = os.environ.get("VEHICLE_SERVICE_ADDR", "localhost:50053")

GRPC_OPTIONS = [
    ("grpc.max_send_message_length", 100 * 1024 * 1024),
    ("grpc.max_receive_message_length", 100 * 1024 * 1024),
]


def decode_frame(frame_msg):
    img = Image.open(io.BytesIO(frame_msg.data)).convert("RGB")
    return np.array(img)


def encode_frame_jpeg(frame_np, quality=85):
    img = Image.fromarray(frame_np)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def merge_annotations(base_frame_bytes, ped_frame_bytes, veh_frame_bytes):
    """
    合并标注: 以原图为底，将 pedestrian 和 vehicle 的标注叠加上去。
    使用简单的非黑区域混合。
    """
    base = np.array(Image.open(io.BytesIO(base_frame_bytes)).convert("RGB"))

    if ped_frame_bytes:
        ped = np.array(Image.open(io.BytesIO(ped_frame_bytes)).convert("RGB"))
        # 找到 ped 中与 base 不同的像素（即标注区域）
        diff = np.any(ped != base, axis=-1)
        base[diff] = ped[diff]

    if veh_frame_bytes:
        veh = np.array(Image.open(io.BytesIO(veh_frame_bytes)).convert("RGB"))
        diff = np.any(veh != base, axis=-1)
        base[diff] = veh[diff]

    return base


class RouterServicer(pipeline_pb2_grpc.RouterServiceServicer):

    def __init__(self):
        logger.info("Loading YOLOv8s detector...")
        t0 = time.time()
        self.detector = YOLO("yolov8s.pt")
        logger.info(f"YOLOv8s loaded in {time.time() - t0:.2f}s")

        logger.info(f"Connecting to Pedestrian Service: {PED_ADDR}")
        self.ped_channel = grpc.insecure_channel(PED_ADDR, options=GRPC_OPTIONS)
        self.ped_stub = pipeline_pb2_grpc.PedestrianServiceStub(self.ped_channel)

        logger.info(f"Connecting to Vehicle Service: {VEH_ADDR}")
        self.veh_channel = grpc.insecure_channel(VEH_ADDR, options=GRPC_OPTIONS)
        self.veh_stub = pipeline_pb2_grpc.VehicleServiceStub(self.veh_channel)

    def Detect(self, request, context):
        start = time.time()

        frame = decode_frame(request.frame)
        frame_jpeg = encode_frame_jpeg(frame)

        # ---- 1) YOLO 检测 ----
        yolo_results = self.detector(frame, verbose=False)[0]

        all_detections = []
        person_dets = []
        vehicle_dets = []
        unhandled_dets = []

        for box in yolo_results.boxes:
            x1, y1, x2, y2 = box.xyxy[0].tolist()
            cls_name = yolo_results.names[int(box.cls)]
            conf = round(float(box.conf), 4)

            det = pipeline_pb2.Detection(
                class_name=cls_name,
                confidence=conf,
                bbox=pipeline_pb2.BoundingBox(x1=x1, y1=y1, x2=x2, y2=y2),
            )
            all_detections.append(det)

            if cls_name == "person":
                person_dets.append(det)
            elif cls_name in VEHICLE_CLASSES:
                vehicle_dets.append(det)
            else:
                unhandled_dets.append(det)

        logger.info(
            f"Detected {len(all_detections)} objects: "
            f"{len(person_dets)} persons, {len(vehicle_dets)} vehicles, "
            f"{len(unhandled_dets)} other"
        )

        # ---- 2) 并行调用下游服务 ----
        frame_msg = pipeline_pb2.Frame(
            data=frame_jpeg,
            width=frame.shape[1],
            height=frame.shape[0],
        )

        ped_future = None
        veh_future = None
        ped_response = None
        veh_response = None

        if person_dets:
            ped_req = pipeline_pb2.PedestrianRequest(
                frame=frame_msg, detections=person_dets
            )
            ped_future = self.ped_stub.ProcessFrame.future(ped_req)

        if vehicle_dets:
            veh_req = pipeline_pb2.VehicleRequest(
                frame=frame_msg, detections=vehicle_dets
            )
            veh_future = self.veh_stub.ProcessFrame.future(veh_req)

        # 等待结果
        if ped_future:
            try:
                ped_response = ped_future.result(timeout=30)
            except Exception as e:
                logger.error(f"Pedestrian service error: {e}")

        if veh_future:
            try:
                veh_response = veh_future.result(timeout=30)
            except Exception as e:
                logger.error(f"Vehicle service error: {e}")

        # ---- 3) 合并标注图 ----
        merged = merge_annotations(
            frame_jpeg,
            ped_response.annotated_frame if ped_response else None,
            veh_response.annotated_frame if veh_response else None,
        )
        merged_jpeg = encode_frame_jpeg(merged)

        total_latency = round((time.time() - start) * 1000, 1)
        logger.info(f"Total pipeline latency: {total_latency}ms")

        return pipeline_pb2.RouterResponse(
            all_detections=all_detections,
            pedestrian_result=ped_response,
            vehicle_result=veh_response,
            unhandled=unhandled_dets,
            merged_frame=merged_jpeg,
            total_latency_ms=total_latency,
        )


def serve():
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=4),
        options=GRPC_OPTIONS,
    )
    pipeline_pb2_grpc.add_RouterServiceServicer_to_server(
        RouterServicer(), server
    )
    server.add_insecure_port("[::]:50051")
    logger.info("Router service listening on :50051")
    server.start()
    server.wait_for_termination()


if __name__ == "__main__":
    serve()
