"""
Export YOLOv8 models to ONNX for Triton Inference Server.

Run once before starting Triton:
    python scripts/export_models.py

Outputs:
    triton-models/yolov8s/1/model.onnx       (detection, fixed batch=1)
    triton-models/yolov8s-pose/1/model.onnx  (pose, dynamic batch)
"""

import shutil
from pathlib import Path
from ultralytics import YOLO

REPO = Path(__file__).parent.parent / "triton-models"


def export(model_name: str, dest_dir: Path, dynamic: bool):
    print(f"Exporting {model_name} (dynamic={dynamic})...")
    model = YOLO(f"{model_name}.pt")
    out = model.export(format="onnx", dynamic=dynamic, simplify=True, imgsz=640)
    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.move(out, dest_dir / "model.onnx")
    print(f"  → {dest_dir / 'model.onnx'}")


if __name__ == "__main__":
    export("yolov8s",      REPO / "yolov8s"      / "1", dynamic=False)
    export("yolov8s-pose", REPO / "yolov8s-pose" / "1", dynamic=True)
    print("Done. Start Triton with: tritonserver --model-repository=triton-models/")
