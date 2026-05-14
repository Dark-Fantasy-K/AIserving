FROM nvidia/cuda:12.1.1-cudnn8-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3.11 python3-pip \
        libglib2.0-0 libgl1 libgomp1 \
    && ln -sf /usr/bin/python3.11 /usr/bin/python3 \
    && ln -sf /usr/bin/python3 /usr/bin/python \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip3 install --no-cache-dir torch torchvision --index-url https://download.pytorch.org/whl/cu121

COPY services/router-service/requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

COPY pipeline.proto .
RUN mkdir -p proto_gen && \
    python3 -m grpc_tools.protoc \
        -I . \
        --python_out=proto_gen \
        --pyi_out=proto_gen \
        --grpc_python_out=proto_gen \
        pipeline.proto && \
    sed -i 's/^import pipeline_pb2/from . import pipeline_pb2/' proto_gen/pipeline_pb2_grpc.py && \
    touch proto_gen/__init__.py

COPY telemetry.py .
COPY services/router-service/server.py .

EXPOSE 50051
CMD ["python", "server.py"]
