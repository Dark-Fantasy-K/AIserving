"""
Pedestrian gRPC Service
───────────────────────
ProcessCrops (primary): receives small person crop images, runs pose on each crop
  via Triton Inference Server, returns keypoints mapped back to original-frame coordinates.
ProcessFrame (legacy): full-frame pose + annotation, kept for backward compat.
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
import tritonclient.grpc as triton_grpc

from proto_gen import pipeline_pb2, pipeline_pb2_grpc
from telemetry import setup, grpc_extract

logging.basicConfig(level=logging.INFO, format="%(asctime)s [PED] %(message)s")
logger = logging.getLogger(__name__)

tracer, meter = setup("pedestrian")
_response_time   = meter.create_histogram("pedestrian.response_time_ms",   unit="ms", description="Full RPC duration")
_processing_time = meter.create_histogram("pedestrian.processing_time_ms", unit="ms", description="Pose inference duration")

CONF_THRESH = float(os.environ.get("CONF_THRESH", "0.25"))
IOU_THRESH  = float(os.environ.get("IOU_THRESH",  "0.45"))

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
    """Returns CHW float32 tensor (no batch dim) and scaling params."""
    h, w = img_rgb.shape[:2]
    scale = size / max(h, w)
    nh, nw = int(h * scale), int(w * scale)
    resized = cv2.resize(img_rgb, (nw, nh))
    canvas = np.full((size, size, 3), 114, dtype=np.uint8)
    py, px = (size - nh) // 2, (size - nw) // 2
    canvas[py:py + nh, px:px + nw] = resized
    tensor = canvas.astype(np.float32) / 255.0
    tensor = tensor.transpose(2, 0, 1)  # [3, 640, 640]
    return tensor, scale, px, py


def decode_poses(output0: np.ndarray, scale: float, pad_x: int, pad_y: int):
    """
    output0: [56, 8400] for one crop  (cx,cy,w,h, conf, 17×(kx,ky,kv))
    Returns list of dicts: best detection per crop (highest conf after NMS).
    """
    pred = output0.T  # [8400, 56]
    cx, cy, bw, bh = pred[:, 0], pred[:, 1], pred[:, 2], pred[:, 3]
    confs = pred[:, 4]
    kpts = pred[:, 5:].reshape(-1, 17, 3)  # [8400, 17, 3]

    mask = confs > CONF_THRESH
    if not mask.any():
        return []

    cx, cy, bw, bh = cx[mask], cy[mask], bw[mask], bh[mask]
    confs = confs[mask]
    kpts = kpts[mask]

    x1 = (cx - bw / 2 - pad_x) / scale
    y1 = (cy - bh / 2 - pad_y) / scale
    x2 = (cx + bw / 2 - pad_x) / scale
    y2 = (cy + bh / 2 - pad_y) / scale

    boxes_xywh = np.stack([x1, y1, x2 - x1, y2 - y1], axis=1)
    idxs = cv2.dnn.NMSBoxes(boxes_xywh.tolist(), confs.tolist(), CONF_THRESH, IOU_THRESH)
    if len(idxs) == 0:
        return []

    results = []
    for i in idxs.flatten():
        kp_list = []
        for j in range(17):
            kx = (kpts[i, j, 0] - pad_x) / scale
            ky = (kpts[i, j, 1] - pad_y) / scale
            kv = float(kpts[i, j, 2])
            kp_list.append((float(kx), float(ky), kv))
        results.append({
            "x1": float(x1[i]), "y1": float(y1[i]),
            "x2": float(x2[i]), "y2": float(y2[i]),
            "conf": float(confs[i]),
            "keypoints": kp_list,
        })
    return results


# ── Helpers ────────────────────────────────────────────────────────────────────

def decode_image_bytes(data):
    return np.array(Image.open(io.BytesIO(data)).convert("RGB"))


def encode_frame(frame_np, quality=85):
    buf = io.BytesIO()
    Image.fromarray(frame_np).save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


# ── Service ────────────────────────────────────────────────────────────────────

class PedestrianServicer(pipeline_pb2_grpc.PedestrianServiceServicer):

    def __init__(self):
        url = os.environ.get("TRITON_URL", "triton:8001")
        model = os.environ.get("POSE_MODEL", "yolov8s-pose")
        logger.info(f"Connecting to Triton at {url}...")
        self.triton = triton_grpc.InferenceServerClient(url=url)
        self.pose_model = model
        self._wait_for_model(model)
        logger.info(f"Model '{model}' ready")

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

    def _infer_pose_batch(self, crops_np: list):
        """
        Preprocess N crops, call Triton with batch [N,3,640,640],
        return list of per-crop pose results (in crop-local coordinates).
        """
        tensors = []
        metas = []
        for img in crops_np:
            t, scale, px, py = letterbox(img)
            tensors.append(t)
            metas.append((scale, px, py))

        batch = np.stack(tensors)  # [N, 3, 640, 640]
        n = batch.shape[0]

        inp = triton_grpc.InferInput("images", list(batch.shape), "FP32")
        inp.set_data_from_numpy(batch)
        out = triton_grpc.InferRequestedOutput("output0")
        resp = self.triton.infer(self.pose_model, [inp], outputs=[out])
        output0 = resp.as_numpy("output0")  # [N, 56, 8400]

        results = []
        for i in range(n):
            scale, px, py = metas[i]
            poses = decode_poses(output0[i], scale, px, py)
            results.append(poses)
        return results

    # ------------------------------------------------------------------
    # PRIMARY: crop-based — Router sends small person ROIs
    # ------------------------------------------------------------------
    def ProcessCrops(self, request, context):
        parent_ctx = grpc_extract(context)
        t_start = time.time()

        with tracer.start_as_current_span("pedestrian.process_crops", context=parent_ctx) as span:
            n_crops = len(request.crops)
            if n_crops == 0:
                return pipeline_pb2.PedestrianCropResponse(poses=[])

            logger.info(f"Received {n_crops} crop(s) — batch inference")
            span.set_attribute("crop_count", n_crops)

            crops_np = [decode_image_bytes(c.data) for c in request.crops]

            t_inf = time.time()
            batch_results = self._infer_pose_batch(crops_np)
            inf_ms = round((time.time() - t_inf) * 1000, 1)
            _processing_time.record(inf_ms)
            logger.info(f"  Batch inference {n_crops} crops: {inf_ms}ms (avg {inf_ms/n_crops:.1f}ms/crop)")

            poses = []
            for crop_msg, crop_poses in zip(request.crops, batch_results):
                if not crop_poses:
                    logger.info(f"  crop_id={crop_msg.crop_id}: no pose found")
                    continue

                # pick highest-confidence detection
                best = max(crop_poses, key=lambda p: p["conf"])
                cb = crop_msg.crop_bbox

                proto_kps = []
                for j, name in enumerate(KEYPOINT_NAMES):
                    kx, ky, kc = best["keypoints"][j]
                    proto_kps.append(pipeline_pb2.Keypoint(
                        name=name,
                        x=round(cb.x1 + kx, 1),
                        y=round(cb.y1 + ky, 1),
                        confidence=round(kc, 3),
                    ))

                poses.append(pipeline_pb2.PersonPoseResult(
                    crop_id=crop_msg.crop_id,
                    keypoints=proto_kps,
                    confidence=round(best["conf"], 4),
                    bbox=pipeline_pb2.BoundingBox(
                        x1=cb.x1 + best["x1"], y1=cb.y1 + best["y1"],
                        x2=cb.x1 + best["x2"], y2=cb.y1 + best["y2"],
                    ),
                ))

            elapsed = round((time.time() - t_start) * 1000, 1)
            span.set_attribute("pose_count", len(poses))
            span.set_attribute("response_time_ms", elapsed)
            _response_time.record(elapsed)
            logger.info(f"ProcessCrops: {len(poses)}/{n_crops} poses in {elapsed}ms")

            return pipeline_pb2.PedestrianCropResponse(poses=poses)

    # ------------------------------------------------------------------
    # LEGACY: full-frame — kept for backward compat, not called by Router
    # ------------------------------------------------------------------
    def ProcessFrame(self, request, context):
        parent_ctx = grpc_extract(context)
        t_start = time.time()

        with tracer.start_as_current_span("pedestrian.process_frame", context=parent_ctx) as span:
            frame = decode_image_bytes(request.frame.data)

            with tracer.start_as_current_span("pedestrian.pose_inference") as proc_span:
                t_proc = time.time()
                results = self._infer_pose_batch([frame])
                proc_ms = round((time.time() - t_proc) * 1000, 1)
                proc_span.set_attribute("processing_time_ms", proc_ms)
                _processing_time.record(proc_ms)

            persons   = []
            annotated = frame.copy()
            frame_poses = results[0] if results else []

            for p in frame_poses:
                x1, y1, x2, y2 = int(p["x1"]), int(p["y1"]), int(p["x2"]), int(p["y2"])
                conf = p["conf"]

                proto_kps = []
                pts = []
                for j, name in enumerate(KEYPOINT_NAMES):
                    x, y, c = p["keypoints"][j]
                    proto_kps.append(pipeline_pb2.Keypoint(
                        name=name, x=round(x, 1), y=round(y, 1), confidence=round(c, 3)
                    ))
                    pts.append((int(x), int(y), c))

                persons.append(pipeline_pb2.PersonPose(
                    bbox=pipeline_pb2.BoundingBox(x1=x1, y1=y1, x2=x2, y2=y2),
                    confidence=round(conf, 4),
                    keypoints=proto_kps,
                ))

                cv2.rectangle(annotated, (x1, y1), (x2, y2), COLOR_BBOX, 2)
                label = f'person {conf:.0%}'
                (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
                cv2.rectangle(annotated, (x1, y1 - th - 8), (x1 + tw + 8, y1), COLOR_TEXT_BG, -1)
                cv2.putText(annotated, label, (x1 + 4, y1 - 4),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, COLOR_BBOX, 1, cv2.LINE_AA)

                for a, b in SKELETON:
                    if pts[a][2] > 0.3 and pts[b][2] > 0.3:
                        cv2.line(annotated, pts[a][:2], pts[b][:2], COLOR_SKEL, 2, cv2.LINE_AA)
                for px, py, pc in pts:
                    if pc > 0.3:
                        cv2.circle(annotated, (px, py), 4, COLOR_KP, -1, cv2.LINE_AA)

            elapsed = round((time.time() - t_start) * 1000, 1)
            span.set_attribute("response_time_ms", elapsed)
            span.set_attribute("person_count", len(persons))
            _response_time.record(elapsed)
            logger.info(f"ProcessFrame: {len(persons)} persons in {elapsed}ms")

            return pipeline_pb2.PedestrianResponse(
                person_count=len(persons),
                persons=persons,
                annotated_frame=encode_frame(annotated),
            )


def serve():
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=4),
        options=[
            ("grpc.max_send_message_length", 100 * 1024 * 1024),
            ("grpc.max_receive_message_length", 100 * 1024 * 1024),
        ],
    )
    pipeline_pb2_grpc.add_PedestrianServiceServicer_to_server(PedestrianServicer(), server)
    server.add_insecure_port("[::]:50052")
    logger.info("Pedestrian service listening on :50052")
    server.start()
    server.wait_for_termination()


if __name__ == "__main__":
    serve()
