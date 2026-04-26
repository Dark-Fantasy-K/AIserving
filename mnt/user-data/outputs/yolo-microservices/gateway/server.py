"""
HTTP Gateway
────────────
Flask 对外提供 HTTP + Web UI，内部调用 Router gRPC 获取结果。
这是唯一暴露给用户的入口。
"""

import io
import os
import time
import base64
import logging

import grpc
from flask import Flask, request, jsonify, render_template
from PIL import Image

from proto_gen import pipeline_pb2, pipeline_pb2_grpc

logging.basicConfig(level=logging.INFO, format="%(asctime)s [GW] %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)

ROUTER_ADDR = os.environ.get("ROUTER_SERVICE_ADDR", "localhost:50051")
GRPC_OPTIONS = [
    ("grpc.max_send_message_length", 100 * 1024 * 1024),
    ("grpc.max_receive_message_length", 100 * 1024 * 1024),
]

logger.info(f"Connecting to Router: {ROUTER_ADDR}")
channel = grpc.insecure_channel(ROUTER_ADDR, options=GRPC_OPTIONS)
router_stub = pipeline_pb2_grpc.RouterServiceStub(channel)


@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "service": "gateway",
        "router": ROUTER_ADDR,
    })


@app.route("/predict", methods=["POST"])
def predict():
    if "image" not in request.files:
        return jsonify({"error": "no image field"}), 400

    file = request.files["image"]
    img_bytes = file.read()

    # 获取尺寸
    img = Image.open(io.BytesIO(img_bytes))
    w, h = img.size

    frame = pipeline_pb2.Frame(data=img_bytes, width=w, height=h)
    req = pipeline_pb2.RouterRequest(frame=frame)

    try:
        resp = router_stub.Detect(req, timeout=60)
    except grpc.RpcError as e:
        logger.error(f"gRPC error: {e}")
        return jsonify({"error": f"Router error: {e.details()}"}), 502

    # 构造 JSON 响应
    result = {
        "total_latency_ms": resp.total_latency_ms,
        "total_detections": len(resp.all_detections),
        "annotated_img": f"data:image/jpeg;base64,{base64.b64encode(resp.merged_frame).decode()}",
        "unhandled": [
            {"class": d.class_name, "confidence": round(d.confidence, 4),
             "bbox": [round(d.bbox.x1, 1), round(d.bbox.y1, 1),
                      round(d.bbox.x2, 1), round(d.bbox.y2, 1)]}
            for d in resp.unhandled
        ],
    }

    # pedestrian
    if resp.HasField("pedestrian_result"):
        pr = resp.pedestrian_result
        result["PersonPoseHandler"] = {
            "task": "pose_estimation",
            "person_count": pr.person_count,
            "latency_ms": pr.latency_ms,
            "persons": [
                {
                    "confidence": round(p.confidence, 4),
                    "bbox": [round(p.bbox.x1, 1), round(p.bbox.y1, 1),
                             round(p.bbox.x2, 1), round(p.bbox.y2, 1)],
                    "keypoints": {
                        kp.name: {"x": kp.x, "y": kp.y, "confidence": kp.confidence}
                        for kp in p.keypoints
                    },
                }
                for p in pr.persons
            ],
        }

    # vehicle
    if resp.HasField("vehicle_result"):
        vr = resp.vehicle_result
        result["VehicleCountHandler"] = {
            "task": "vehicle_counting",
            "current_total": vr.current_total,
            "active_tracks": vr.active_tracks,
            "latency_ms": vr.latency_ms,
            "vehicles": [
                {
                    "class": v.class_name,
                    "confidence": round(v.confidence, 4),
                    "bbox": [round(v.bbox.x1, 1), round(v.bbox.y1, 1),
                             round(v.bbox.x2, 1), round(v.bbox.y2, 1)],
                    "track_id": v.track_id,
                }
                for v in vr.vehicles
            ],
            "current_counts": {c.class_name: c.count for c in vr.current_counts},
            "cumulative": {c.class_name: c.count for c in vr.cumulative},
        }

    return jsonify(result)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
