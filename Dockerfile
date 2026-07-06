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

COPY server/ ./server/
COPY shared/ ./shared/
COPY --from=web /src/web/dist ./web/dist

ENV DATA_DIR=/data
VOLUME ["/data"]
EXPOSE 8000

CMD ["uvicorn", "server.main:app", "--host", "0.0.0.0", "--port", "8000"]
