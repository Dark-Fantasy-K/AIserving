"""
Router gRPC Service
───────────────────
1. Run YOLOv8s detection on the full frame.
2. Route by class: person → Pedestrian Service, vehicles → Vehicle Service.
3. Call both downstream services in parallel.
4. Merge annotated frames and return combined results.
"""

import io
import os
import time
import logging
import threading
from concurrent import futures

import cv2
import grpc
import numpy as np
import torch
from PIL import Image
from ultralytics import YOLO

DEVICE = 0 if torch.cuda.is_available() else "cpu"

from proto_gen import pipeline_pb2, pipeline_pb2_grpc
from telemetry import setup, grpc_inject, grpc_extract

logging.basicConfig(level=logging.INFO, format="%(asctime)s [RTR] %(message)s")
logger = logging.getLogger(__name__)

tracer, meter = setup("router")
_response_time   = meter.create_histogram("router.response_time_ms",   unit="ms", description="Full Detect RPC duration")
_processing_time = meter.create_histogram("router.processing_time_ms", unit="ms", description="YOLO inference duration")

VEHICLE_CLASSES = {"car", "truck", "bus", "motorcycle"}

# Support comma-separated list for sharding across multiple pedestrian instances.
# E.g. PEDESTRIAN_SERVICE_ADDRS="svc0:50052,svc1:50052"
# Falls back to single PEDESTRIAN_SERVICE_ADDR for backward compatibility.
_PED_ADDRS_ENV = os.environ.get("PEDESTRIAN_SERVICE_ADDRS", "")
PED_ADDRS = [a.strip() for a in _PED_ADDRS_ENV.split(",") if a.strip()] or \
            [os.environ.get("PEDESTRIAN_SERVICE_ADDR", "localhost:50052")]

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


def _merge_ped_frames(base_frame_bytes, ped_frames_list):
    """Merge annotated frames from multiple pedestrian service shards into one."""
    if not ped_frames_list:
        return base_frame_bytes
    base = np.array(Image.open(io.BytesIO(base_frame_bytes)).convert("RGB"))
    for ann_bytes in ped_frames_list:
        ann = np.array(Image.open(io.BytesIO(ann_bytes)).convert("RGB"))
        diff = np.any(ann != base, axis=-1)
        base[diff] = ann[diff]
    img = Image.fromarray(base)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def merge_annotations(base_frame_bytes, ped_frame_bytes, veh_frame_bytes):
    base = np.array(Image.open(io.BytesIO(base_frame_bytes)).convert("RGB"))

    if ped_frame_bytes:
        ped = np.array(Image.open(io.BytesIO(ped_frame_bytes)).convert("RGB"))
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
        self.detector.to(DEVICE)
        self._infer_lock = threading.Lock()
        logger.info(f"YOLOv8s loaded in {time.time() - t0:.2f}s (device={DEVICE})")

        logger.info(f"Connecting to {len(PED_ADDRS)} Pedestrian Service(s): {PED_ADDRS}")
        self.ped_stubs = []
        for addr in PED_ADDRS:
            ch = grpc.insecure_channel(addr, options=GRPC_OPTIONS)
            self.ped_stubs.append(pipeline_pb2_grpc.PedestrianServiceStub(ch))

        logger.info(f"Connecting to Vehicle Service: {VEH_ADDR}")
        self.veh_channel = grpc.insecure_channel(VEH_ADDR, options=GRPC_OPTIONS)
        self.veh_stub = pipeline_pb2_grpc.VehicleServiceStub(self.veh_channel)

    def Detect(self, request, context):
        parent_ctx = grpc_extract(context)
        t_start = time.time()

        with tracer.start_as_current_span("router.detect", context=parent_ctx) as span:
            frame = decode_frame(request.frame)
            frame_jpeg = encode_frame_jpeg(frame)

            # ---- 1) YOLO detection ----
            with tracer.start_as_current_span("router.yolo_inference") as proc_span:
                t_proc = time.time()
                with self._infer_lock:
                    yolo_results = self.detector(frame, verbose=False, device=DEVICE)[0]
                proc_ms = round((time.time() - t_proc) * 1000, 1)
                proc_span.set_attribute("processing_time_ms", proc_ms)
                _processing_time.record(proc_ms)
                logger.info(f"YOLO inference: {proc_ms}ms")

            all_detections = []
            person_dets = []
            vehicle_dets = []
            unhandled_dets = []
            ped_id_counter = 0

            for box in yolo_results.boxes:
                x1, y1, x2, y2 = box.xyxy[0].tolist()
                cls_name = yolo_results.names[int(box.cls)]
                conf = round(float(box.conf), 4)

                ped_id = -1
                if cls_name == "person":
                    ped_id = ped_id_counter
                    ped_id_counter += 1

                det = pipeline_pb2.Detection(
                    class_name=cls_name,
                    confidence=conf,
                    bbox=pipeline_pb2.BoundingBox(x1=x1, y1=y1, x2=x2, y2=y2),
                    pedestrian_id=ped_id,
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

            # ---- 2) dispatch to downstream services in parallel ----
            frame_msg = pipeline_pb2.Frame(
                data=frame_jpeg,
                width=frame.shape[1],
                height=frame.shape[0],
            )

            downstream_meta = grpc_inject()
            veh_future = None
            veh_response = None

            # Shard persons across pedestrian service instances by pedestrian_id % N
            n_ped = len(self.ped_stubs)
            ped_shards = [[] for _ in range(n_ped)]
            for det in person_dets:
                ped_shards[det.pedestrian_id % n_ped].append(det)

            ped_futures = []
            for shard_idx, (stub, shard_dets) in enumerate(zip(self.ped_stubs, ped_shards)):
                if shard_dets:
                    ped_req = pipeline_pb2.PedestrianRequest(
                        frame=frame_msg, detections=shard_dets
                    )
                    ped_futures.append(stub.ProcessFrame.future(ped_req, metadata=downstream_meta))
                    logger.info(f"Shard {shard_idx}: {len(shard_dets)} persons → {PED_ADDRS[shard_idx]}")
                else:
                    ped_futures.append(None)

            if vehicle_dets:
                veh_req = pipeline_pb2.VehicleRequest(
                    frame=frame_msg, detections=vehicle_dets
                )
                veh_future = self.veh_stub.ProcessFrame.future(veh_req, metadata=downstream_meta)

            # Collect all pedestrian shard responses
            all_persons = []
            ped_annotated_frames = []
            for shard_idx, fut in enumerate(ped_futures):
                if fut is None:
                    continue
                try:
                    resp = fut.result(timeout=30)
                    all_persons.extend(resp.persons)
                    if resp.annotated_frame:
                        ped_annotated_frames.append(resp.annotated_frame)
                except Exception as e:
                    logger.error(f"Pedestrian service[{shard_idx}] error: {e}")

            ped_response = pipeline_pb2.PedestrianResponse(
                person_count=len(all_persons),
                persons=all_persons,
                annotated_frame=_merge_ped_frames(frame_jpeg, ped_annotated_frames),
            ) if all_persons or ped_annotated_frames else None

            if veh_future:
                try:
                    veh_response = veh_future.result(timeout=30)
                except Exception as e:
                    logger.error(f"Vehicle service error: {e}")

            # ---- 3) merge annotated frames ----
            merged = merge_annotations(
                frame_jpeg,
                ped_response.annotated_frame if ped_response else None,
                veh_response.annotated_frame if veh_response else None,
            )
            merged_jpeg = encode_frame_jpeg(merged)

            elapsed = round((time.time() - t_start) * 1000, 1)
            span.set_attribute("response_time_ms", elapsed)
            span.set_attribute("persons_detected", len(person_dets))
            span.set_attribute("vehicles_detected", len(vehicle_dets))
            _response_time.record(elapsed)

            logger.info(f"Total pipeline latency: {elapsed}ms")

            return pipeline_pb2.RouterResponse(
                all_detections=all_detections,
                pedestrian_result=ped_response,
                vehicle_result=veh_response,
                unhandled=unhandled_dets,
                merged_frame=merged_jpeg,
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
