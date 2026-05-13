"""
HTTP Gateway
────────────
Flask HTTP + Web UI frontend; proxies requests to the Router via gRPC.
This is the sole external entry point exposed to clients.
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
from telemetry import setup, grpc_inject

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

tracer, meter = setup("gateway")
_response_time     = meter.create_histogram("gateway.response_time_ms",     unit="ms", description="Full HTTP request duration")
_transaction_time  = meter.create_histogram("pipeline.transaction_time_ms", unit="ms", description="End-to-end pipeline duration")


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
    t_start = time.time()
    with tracer.start_as_current_span("gateway.predict") as span:
        if request.content_type == "image/jpeg":
            img_bytes = request.data
        elif "image" in request.files:
            img_bytes = request.files["image"].read()
        else:
            return jsonify({"error": "no image"}), 400

        img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
        img.thumbnail((640, 640), Image.BILINEAR)
        w, h = img.size
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        img_bytes = buf.getvalue()

        frame = pipeline_pb2.Frame(data=img_bytes, width=w, height=h)
        req = pipeline_pb2.RouterRequest(frame=frame)

        try:
            resp = router_stub.Detect(req, timeout=10, metadata=grpc_inject())
        except grpc.RpcError as e:
            logger.error(f"gRPC error: {e}")
            elapsed = (time.time() - t_start) * 1000
            span.set_attribute("error", True)
            _response_time.record(elapsed)
            _transaction_time.record(elapsed)
            return jsonify({"error": f"Router error: {e.details()}"}), 502

        slim = request.args.get("slim")
        result = {
            "total_detections": len(resp.all_detections),
            "annotated_img": "" if slim else f"data:image/jpeg;base64,{base64.b64encode(resp.merged_frame).decode()}",
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

        elapsed = round((time.time() - t_start) * 1000, 1)
        result["processing_ms"] = elapsed
        span.set_attribute("response_time_ms", elapsed)
        span.set_attribute("transaction_time_ms", elapsed)
        _response_time.record(elapsed)
        _transaction_time.record(elapsed)

        return jsonify(result)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
