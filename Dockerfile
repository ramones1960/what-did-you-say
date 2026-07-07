# --- フロントエンドのビルド ---
FROM node:22-slim AS web
WORKDIR /src/web
COPY web/package.json web/package-lock.json* ./
RUN npm install
# shared/ は結合パラメータの正典 (merge_params.json)。web/src/lib.ts が相対パスで参照する
COPY shared/ ../shared/
COPY web/ ./
RUN npm run build

# --- アプリ本体 ---
FROM python:3.11-slim
WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# GPU=1 のとき、faster-whisper (CTranslate2) の CUDA 実行に必要な
# cuBLAS / cuDNN を pip で追加する(docker-compose.gpu.yml が指定する)。
# ベースイメージは CUDA を含まない slim のため、これが無いと
# WHISPER_DEVICE=cuda は推論時のライブラリロードで失敗する。
# NVIDIA Container Toolkit が注入するのはドライバ層のみで cuBLAS/cuDNN は含まれない
ARG GPU=0
RUN if [ "$GPU" = "1" ]; then \
        pip install --no-cache-dir nvidia-cublas-cu12 "nvidia-cudnn-cu12==9.*"; \
    fi
# CTranslate2 が cuBLAS/cuDNN を見つけられるようにする(CPU ビルドでは
# ディレクトリが存在しないだけで無害)
ENV LD_LIBRARY_PATH=/usr/local/lib/python3.11/site-packages/nvidia/cublas/lib:/usr/local/lib/python3.11/site-packages/nvidia/cudnn/lib

COPY server/ ./server/
COPY shared/ ./shared/
COPY --from=web /src/web/dist ./web/dist

ENV DATA_DIR=/data
VOLUME ["/data"]
EXPOSE 8000

CMD ["uvicorn", "server.main:app", "--host", "0.0.0.0", "--port", "8000"]
