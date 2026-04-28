FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        libglib2.0-0 libgl1 libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY mnt/user-data/outputs/yolo-microservices/pedestrian-service/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY pipeline.proto .
RUN mkdir -p proto_gen && \
    python -m grpc_tools.protoc \
        -I . \
        --python_out=proto_gen \
        --pyi_out=proto_gen \
        --grpc_python_out=proto_gen \
        pipeline.proto && \
    sed -i 's/^import pipeline_pb2/from . import pipeline_pb2/' proto_gen/pipeline_pb2_grpc.py && \
    touch proto_gen/__init__.py

COPY mnt/user-data/outputs/yolo-microservices/pedestrian-service/server.py .

EXPOSE 50052
CMD ["python", "server.py"]
