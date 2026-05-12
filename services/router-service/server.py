"""
Router gRPC Service
───────────────────
1. Run YOLOv8s detection via Triton Inference Server.
2. Route by class: person → Pedestrian Service (crop-based), vehicles → Vehicle Service.
3. Call both downstream services in parallel.
4. Draw pedestrian pose results + merge vehicle annotations onto original frame.
"""

import io
import os
import itertools
import time
import logging
from concurrent import futures

import cv2
import grpc
import numpy as np
from PIL import Image
import tritonclient.grpc as triton_grpc

from proto_gen import pipeline_pb2, pipeline_pb2_grpc
from telemetry import setup, grpc_inject, grpc_extract

logging.basicConfig(level=logging.INFO, format="%(asctime)s [RTR] %(message)s")
logger = logging.getLogger(__name__)

tracer, meter = setup("router")
_response_time   = meter.create_histogram("router.response_time_ms",   unit="ms", description="Full Detect RPC duration")
_processing_time = meter.create_histogram("router.processing_time_ms", unit="ms", description="YOLO inference duration")

VEHICLE_CLASSES = {"car", "truck", "bus", "motorcycle"}

TRITON_URL            = os.environ.get("TRITON_URL",             "triton:8001")
DET_MODEL             = os.environ.get("DET_MODEL",              "yolov8s")
PED_ADDR              = os.environ.get("PEDESTRIAN_SERVICE_ADDR", "localhost:50052")
VEH_ADDR              = os.environ.get("VEHICLE_SERVICE_ADDR",    "localhost:50053")
PED_CHANNELS          = int(os.environ.get("PED_CHANNELS",          "4"))
PED_CROP_CHUNK_SIZE   = int(os.environ.get("PED_CROP_CHUNK_SIZE",   "32"))
PED_CROP_PADDING_RATIO = float(os.environ.get("PED_CROP_PADDING_RATIO", "0.15"))
CONF_THRESH           = float(os.environ.get("CONF_THRESH", "0.25"))
IOU_THRESH            = float(os.environ.get("IOU_THRESH",  "0.45"))

GRPC_OPTIONS = [
    ("grpc.max_send_message_length", 100 * 1024 * 1024),
    ("grpc.max_receive_message_length", 100 * 1024 * 1024),
]

# COCO class names (80 classes, index = class_id)
COCO_NAMES = (
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train",
    "truck", "boat", "traffic light", "fire hydrant", "stop sign",
    "parking meter", "bench", "bird", "cat", "dog", "horse", "sheep", "cow",
    "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella", "handbag",
    "tie", "suitcase", "frisbee", "skis", "snowboard", "sports ball", "kite",
    "baseball bat", "baseball glove", "skateboard", "surfboard", "tennis racket",
    "bottle", "wine glass", "cup", "fork", "knife", "spoon", "bowl", "banana",
    "apple", "sandwich", "orange", "broccoli", "carrot", "hot dog", "pizza",
    "donut", "cake", "chair", "couch", "potted plant", "bed", "dining table",
    "toilet", "tv", "laptop", "mouse", "remote", "keyboard", "cell phone",
    "microwave", "oven", "toaster", "sink", "refrigerator", "book", "clock",
    "vase", "scissors", "teddy bear", "hair drier", "toothbrush",
)

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
COLOR_KP       = (110, 231, 183)
COLOR_SKEL     = (80,  180, 140)
COLOR_BBOX     = (110, 231, 183)
COLOR_TEXT_BG  = (18,  18,  26)


# ── Pre/post processing ────────────────────────────────────────────────────────

def letterbox(img_rgb: np.ndarray, size: int = 640):
    """Resize with padding to square. Returns tensor [1,3,H,W] and scaling params."""
    h, w = img_rgb.shape[:2]
    scale = size / max(h, w)
    nh, nw = int(h * scale), int(w * scale)
    resized = cv2.resize(img_rgb, (nw, nh))
    canvas = np.full((size, size, 3), 114, dtype=np.uint8)
    py, px = (size - nh) // 2, (size - nw) // 2
    canvas[py:py + nh, px:px + nw] = resized
    tensor = canvas.astype(np.float32) / 255.0
    tensor = tensor.transpose(2, 0, 1)[np.newaxis]  # [1, 3, 640, 640]
    return tensor, scale, px, py


def decode_detections(output0: np.ndarray, orig_h: int, orig_w: int,
                       scale: float, pad_x: int, pad_y: int):
    """
    output0: [1, 84, 8400]  (cx, cy, w, h, score×80)
    Returns list of dicts with x1,y1,x2,y2,conf,class_name in original-image coords.
    """
    pred = output0[0].T  # [8400, 84]
    cx, cy, bw, bh = pred[:, 0], pred[:, 1], pred[:, 2], pred[:, 3]
    class_scores = pred[:, 4:]
    class_ids = class_scores.argmax(axis=1)
    confs = class_scores.max(axis=1)

    mask = confs > CONF_THRESH
    cx, cy, bw, bh = cx[mask], cy[mask], bw[mask], bh[mask]
    confs = confs[mask]
    class_ids = class_ids[mask]

    x1 = np.clip((cx - bw / 2 - pad_x) / scale, 0, orig_w)
    y1 = np.clip((cy - bh / 2 - pad_y) / scale, 0, orig_h)
    x2 = np.clip((cx + bw / 2 - pad_x) / scale, 0, orig_w)
    y2 = np.clip((cy + bh / 2 - pad_y) / scale, 0, orig_h)

    boxes_xywh = np.stack([x1, y1, x2 - x1, y2 - y1], axis=1)
    idxs = cv2.dnn.NMSBoxes(boxes_xywh.tolist(), confs.tolist(), CONF_THRESH, IOU_THRESH)
    if len(idxs) == 0:
        return []

    return [
        {
            "x1": float(x1[i]), "y1": float(y1[i]),
            "x2": float(x2[i]), "y2": float(y2[i]),
            "conf": float(confs[i]),
            "class_name": COCO_NAMES[int(class_ids[i])],
        }
        for i in idxs.flatten()
    ]


# ── Helpers (unchanged from previous version) ─────────────────────────────────

def chunk_list(items, chunk_size):
    for i in range(0, len(items), chunk_size):
        yield items[i:i + chunk_size]


def decode_frame(frame_msg):
    img = Image.open(io.BytesIO(frame_msg.data)).convert("RGB")
    return np.array(img)


def encode_frame_jpeg(frame_np, quality=85):
    buf = io.BytesIO()
    Image.fromarray(frame_np).save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def crop_person(frame_np, det, padding_ratio, img_h, img_w):
    """Crop a person bbox with padding. Returns (jpeg_bytes, crop_bbox_proto) or None."""
    x1, y1, x2, y2 = det.bbox.x1, det.bbox.y1, det.bbox.x2, det.bbox.y2
    bw, bh = x2 - x1, y2 - y1
    if bw <= 0 or bh <= 0:
        return None
    pad_x = bw * padding_ratio
    pad_y = bh * padding_ratio
    cx1 = max(0, int(x1 - pad_x))
    cy1 = max(0, int(y1 - pad_y))
    cx2 = min(img_w, int(x2 + pad_x))
    cy2 = min(img_h, int(y2 + pad_y))
    if cx2 <= cx1 or cy2 <= cy1:
        return None
    crop = frame_np[cy1:cy2, cx1:cx2]
    buf = io.BytesIO()
    Image.fromarray(crop).save(buf, format="JPEG", quality=85)
    crop_bbox = pipeline_pb2.BoundingBox(x1=cx1, y1=cy1, x2=cx2, y2=cy2)
    return buf.getvalue(), crop_bbox


def draw_pose_results(frame_np, pose_results):
    """Draw keypoints and skeleton lines from PersonPoseResult list onto frame_np (in-place)."""
    for pose in pose_results:
        b = pose.bbox
        x1, y1, x2, y2 = int(b.x1), int(b.y1), int(b.x2), int(b.y2)
        cv2.rectangle(frame_np, (x1, y1), (x2, y2), COLOR_BBOX, 2)
        label = f"person {pose.confidence:.0%}"
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
        cv2.rectangle(frame_np, (x1, y1 - th - 8), (x1 + tw + 8, y1), COLOR_TEXT_BG, -1)
        cv2.putText(frame_np, label, (x1 + 4, y1 - 4),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, COLOR_BBOX, 1, cv2.LINE_AA)
        pts = [(int(kp.x), int(kp.y), kp.confidence) for kp in pose.keypoints]
        for a, b_idx in SKELETON:
            if a < len(pts) and b_idx < len(pts) and pts[a][2] > 0.3 and pts[b_idx][2] > 0.3:
                cv2.line(frame_np, pts[a][:2], pts[b_idx][:2], COLOR_SKEL, 2, cv2.LINE_AA)
        for px, py, pc in pts:
            if pc > 0.3:
                cv2.circle(frame_np, (px, py), 4, COLOR_KP, -1, cv2.LINE_AA)


# ── Service ────────────────────────────────────────────────────────────────────

class RouterServicer(pipeline_pb2_grpc.RouterServiceServicer):

    def __init__(self):
        logger.info(f"Connecting to Triton at {TRITON_URL}...")
        self.triton = triton_grpc.InferenceServerClient(url=TRITON_URL)
        self._wait_for_model(DET_MODEL)
        logger.info(f"Model '{DET_MODEL}' ready")

        logger.info(f"Connecting to Pedestrian Service: {PED_ADDR} ({PED_CHANNELS} channels)")
        self.ped_stubs = [
            pipeline_pb2_grpc.PedestrianServiceStub(
                grpc.insecure_channel(PED_ADDR, options=GRPC_OPTIONS)
            )
            for _ in range(PED_CHANNELS)
        ]
        self._ped_rr = itertools.cycle(self.ped_stubs)

        logger.info(f"Connecting to Vehicle Service: {VEH_ADDR}")
        self.veh_channel = grpc.insecure_channel(VEH_ADDR, options=GRPC_OPTIONS)
        self.veh_stub = pipeline_pb2_grpc.VehicleServiceStub(self.veh_channel)

    def _wait_for_model(self, model_name: str, retries: int = 30, interval: float = 2.0):
        for i in range(retries):
            try:
                if self.triton.is_model_ready(model_name):
                    return
            except Exception:
                pass
            logger.info(f"Waiting for model '{model_name}'... ({i+1}/{retries})")
            time.sleep(interval)
        raise RuntimeError(f"Model '{model_name}' not ready after {retries} retries")

    def _infer_detection(self, frame_rgb: np.ndarray):
        tensor, scale, pad_x, pad_y = letterbox(frame_rgb)
        inp = triton_grpc.InferInput("images", [1, 3, 640, 640], "FP32")
        inp.set_data_from_numpy(tensor)
        out = triton_grpc.InferRequestedOutput("output0")
        resp = self.triton.infer(DET_MODEL, [inp], outputs=[out])
        output0 = resp.as_numpy("output0")
        h, w = frame_rgb.shape[:2]
        return decode_detections(output0, h, w, scale, pad_x, pad_y)

    def Detect(self, request, context):
        parent_ctx = grpc_extract(context)
        t_start = time.time()

        with tracer.start_as_current_span("router.detect", context=parent_ctx) as span:
            frame = decode_frame(request.frame)
            frame_jpeg = encode_frame_jpeg(frame)
            img_h, img_w = frame.shape[:2]

            # ---- 1) YOLO detection via Triton ----
            with tracer.start_as_current_span("router.yolo_inference") as proc_span:
                t_proc = time.time()
                dets = self._infer_detection(frame)
                proc_ms = round((time.time() - t_proc) * 1000, 1)
                proc_span.set_attribute("processing_time_ms", proc_ms)
                _processing_time.record(proc_ms)

            all_detections = []
            person_dets    = []
            vehicle_dets   = []
            unhandled_dets = []

            for d in dets:
                det = pipeline_pb2.Detection(
                    class_name=d["class_name"],
                    confidence=round(d["conf"], 4),
                    bbox=pipeline_pb2.BoundingBox(
                        x1=d["x1"], y1=d["y1"], x2=d["x2"], y2=d["y2"]
                    ),
                )
                all_detections.append(det)
                if d["class_name"] == "person":
                    person_dets.append(det)
                elif d["class_name"] in VEHICLE_CLASSES:
                    vehicle_dets.append(det)
                else:
                    unhandled_dets.append(det)

            logger.info(
                f"Detected {len(all_detections)} objects: "
                f"{len(person_dets)} persons, {len(vehicle_dets)} vehicles, "
                f"{len(unhandled_dets)} other"
            )

            # ---- 2) Build person crops ----
            crops = []
            total_crop_bytes = 0
            total_crop_area  = 0
            for crop_id, det in enumerate(person_dets):
                result = crop_person(frame, det, PED_CROP_PADDING_RATIO, img_h, img_w)
                if result is None:
                    logger.warning(f"Skipping invalid crop for person {crop_id}")
                    continue
                crop_bytes, crop_bbox = result
                total_crop_bytes += len(crop_bytes)
                total_crop_area  += (crop_bbox.x2 - crop_bbox.x1) * (crop_bbox.y2 - crop_bbox.y1)
                crops.append(pipeline_pb2.PersonCrop(
                    data=crop_bytes,
                    width=int(crop_bbox.x2 - crop_bbox.x1),
                    height=int(crop_bbox.y2 - crop_bbox.y1),
                    crop_id=crop_id,
                    crop_bbox=crop_bbox,
                ))

            crop_count = len(crops)
            avg_crop_area = round(total_crop_area / crop_count, 1) if crop_count > 0 else 0
            logger.info(f"Built {crop_count} person crops ({total_crop_bytes} bytes total)")

            span.set_attribute("persons_detected",           len(person_dets))
            span.set_attribute("vehicles_detected",          len(vehicle_dets))
            span.set_attribute("pedestrian_crop_count",      crop_count)
            span.set_attribute("pedestrian_crop_chunk_size", PED_CROP_CHUNK_SIZE)
            span.set_attribute("pedestrian_total_crop_bytes", total_crop_bytes)
            span.set_attribute("pedestrian_avg_crop_area",   avg_crop_area)

            # ---- 3) Dispatch crop chunks + vehicle RPC in parallel ----
            crop_chunks = list(chunk_list(crops, PED_CROP_CHUNK_SIZE))
            chunk_count = len(crop_chunks)
            span.set_attribute("pedestrian_crop_chunk_count", chunk_count)

            ped_futures = []
            for idx, chunk in enumerate(crop_chunks):
                ped_req = pipeline_pb2.PedestrianCropRequest(crops=chunk)
                stub = next(self._ped_rr)
                with tracer.start_as_current_span("router.pedestrian_crop_rpc") as cs:
                    cs.set_attribute("chunk_index", idx)
                    cs.set_attribute("chunk_crop_count", len(chunk))
                    future = stub.ProcessCrops.future(ped_req, metadata=grpc_inject())
                ped_futures.append((idx, future))

            veh_future = None
            if vehicle_dets:
                frame_msg = pipeline_pb2.Frame(data=frame_jpeg, width=img_w, height=img_h)
                veh_req = pipeline_pb2.VehicleRequest(frame=frame_msg, detections=vehicle_dets)
                veh_future = self.veh_stub.ProcessFrame.future(veh_req, metadata=grpc_inject())

            # ---- 4) Collect responses ----
            all_poses = []
            failed_chunks = 0
            for idx, future in ped_futures:
                try:
                    resp = future.result(timeout=30)
                    all_poses.extend(resp.poses)
                except Exception as e:
                    logger.error(f"Pedestrian crop chunk {idx} failed: {e}")
                    failed_chunks += 1

            span.set_attribute("pedestrian_successful_chunks", chunk_count - failed_chunks)
            span.set_attribute("pedestrian_failed_chunks",     failed_chunks)

            veh_response = None
            if veh_future:
                try:
                    veh_response = veh_future.result(timeout=30)
                except Exception as e:
                    logger.error(f"Vehicle service error: {e}")

            # ---- 5) Draw results onto original frame ----
            annotated = frame.copy()
            draw_pose_results(annotated, all_poses)

            if veh_response and veh_response.annotated_frame:
                veh_np = np.array(
                    Image.open(io.BytesIO(veh_response.annotated_frame)).convert("RGB")
                )
                diff = np.any(veh_np != frame, axis=-1)
                annotated[diff] = veh_np[diff]

            merged_jpeg = encode_frame_jpeg(annotated)

            combined_ped = None
            if all_poses:
                combined_ped = pipeline_pb2.PedestrianResponse(
                    person_count=len(all_poses),
                    persons=[
                        pipeline_pb2.PersonPose(
                            bbox=p.bbox,
                            confidence=p.confidence,
                            keypoints=p.keypoints,
                        )
                        for p in all_poses
                    ],
                )

            elapsed = round((time.time() - t_start) * 1000, 1)
            span.set_attribute("response_time_ms", elapsed)
            _response_time.record(elapsed)
            logger.info(f"Detect done: {len(all_detections)} dets, {len(all_poses)} poses in {elapsed}ms")

            return pipeline_pb2.RouterResponse(
                all_detections=all_detections,
                pedestrian_result=combined_ped,
                vehicle_result=veh_response,
                unhandled=unhandled_dets,
                merged_frame=merged_jpeg,
            )


def serve():
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=4),
        options=[
            ("grpc.max_send_message_length", 100 * 1024 * 1024),
            ("grpc.max_receive_message_length", 100 * 1024 * 1024),
        ],
    )
    pipeline_pb2_grpc.add_RouterServiceServicer_to_server(RouterServicer(), server)
    server.add_insecure_port("[::]:50051")
    logger.info("Router service listening on :50051")
    server.start()
    server.wait_for_termination()


if __name__ == "__main__":
    serve()
