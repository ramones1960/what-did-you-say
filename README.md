# what did you say? — ローカル文字起こしアプリ

完全ローカルで動く、時刻タグ付き文字起こしアプリです。音声データもモデルも外部サービスには一切送信されません。

- 🎙 **リアルタイム文字起こし** — ブラウザのマイク入力を発話の区切りごとに文字起こし
- 📁 **ファイル文字起こし** — 音声・動画ファイル(mp3 / m4a / wav / mp4 / mov / mkv / webm など)をアップロードして文字起こし
- 🕒 **時刻タグ付き** — すべてのセグメントに `[HH:MM:SS]` 形式の時刻を付与
- 📤 **エクスポート** — TXT(時刻付き)/ SRT / VTT / JSON
- 🐳 **Docker Compose で起動** — WSL2 でも社内サーバーでも同じ手順

## アーキテクチャ

```
[ブラウザ] ── https / wss ──> [Caddy (TLS終端)] ──> [FastAPI]
                                                     ├ realtime.py   WebSocket + Silero VAD + faster-whisper
                                                     ├ jobs.py       ジョブキュー + ワーカー
                                                     ├ SQLite        ジョブ・結果の永続化 (/data)
                                                     └ ffmpeg        動画・音声の変換
```

| コンポーネント | 技術 |
|---|---|
| 文字起こしエンジン | [faster-whisper](https://github.com/SYSTRAN/faster-whisper)(OpenAI Whisper の CTranslate2 実装) |
| 発話区間検出 | Silero VAD(faster-whisper 同梱) |
| バックエンド | Python / FastAPI(REST + WebSocket) |
| フロントエンド | React + Vite(AudioWorklet で 16kHz PCM をストリーミング) |
| リバースプロキシ | Caddy(TLS 終端) |

## クイックスタート(WSL2 / Linux)

前提: Docker(WSL2 では Docker Desktop または WSL 内の Docker Engine)

```bash
git clone <this repo>
cd what-did-you-say
docker compose up -d --build
```

Windows 側のブラウザで **http://localhost:8000** を開きます(WSL2 の localhost フォワーディングでそのまま届きます)。

- 初回のみ Whisper モデル(既定: `small`, 約500MB)を自動ダウンロードします
- モデルは `model_cache` ボリュームに保存され、2回目以降は再利用されます

### GPU(NVIDIA)を使う

WSL2 + NVIDIA ドライバ + [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html) がある場合:

```bash
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build
```

GPU 構成では既定モデルが `large-v3`(高精度)になります。

### 設定(環境変数)

`.env` ファイルまたは環境変数で上書きできます。

| 変数 | 既定値 | 説明 |
|---|---|---|
| `WHISPER_MODEL` | `small` | `tiny` / `base` / `small` / `medium` / `large-v3` など。CPU なら `small` 前後、GPU なら `large-v3` 推奨 |
| `WHISPER_DEVICE` | `auto` | `cpu` / `cuda` |
| `WHISPER_COMPUTE_TYPE` | `auto` | `int8`(CPU向け)/ `float16`(GPU向け) |
| `WHISPER_LANGUAGE` | (自動判定) | 既定の言語コード(例: `ja`) |
| `JOB_WORKERS` | `1` | ファイル文字起こしの並列ワーカー数 |
| `MAX_REALTIME_SESSIONS` | `2` | リアルタイム文字起こしの同時セッション上限 |
| `SITE_ADDRESS` | `localhost` | Caddy が待ち受けるホスト名(社内公開時に変更) |

## 開発(Docker を使わない場合)

```bash
# バックエンド (要: python3.11+, ffmpeg)
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn server.main:app --reload

# フロントエンド (別ターミナル。/api と /ws は 8000 にプロキシされる)
cd web
npm install
npm run dev
```

## 社内ネットワークへの公開

このリポジトリは最初から社内公開を想定した構造になっています。移行時にやることは以下だけです。

1. **サーバーへ移設** — 社内サーバー(Linux)で同じ `docker compose up -d` を実行
2. **ホスト名と証明書** — `SITE_ADDRESS=transcribe.example.internal` を設定。
   ブラウザのマイク取得(`getUserMedia`)は localhost 以外では **HTTPS 必須**のため、必ず Caddy(HTTPS)経由でアクセスさせる。
   証明書は `tls internal`(Caddy のルート証明書を社内配布)か、社内 CA 発行の証明書を `Caddyfile` で指定
3. **認証の差し替え** — `server/auth.py` の `get_current_user` を社内の認証方式(OIDC / LDAP / Basic など)に実装差し替え。全 API はこの関数を通っている
4. **リソース調整** — 利用者数に応じて `JOB_WORKERS` / `MAX_REALTIME_SESSIONS` を調整。
   スケールが必要になったら `server/jobs.py` のインプロセスキューを Redis + 別プロセスワーカーに差し替える
5. **直接ポートの閉塞** — `docker-compose.yml` の `app` サービスの `ports`(8000)を削除し、Caddy 経由のみにする

## ディレクトリ構成

```
├── server/            # FastAPI バックエンド
│   ├── main.py        #   API ルーティング + SPA 配信
│   ├── realtime.py    #   リアルタイム文字起こし (WebSocket + VAD)
│   ├── jobs.py        #   ファイル文字起こしのジョブキュー
│   ├── transcriber.py #   faster-whisper ラッパー
│   ├── media.py       #   ffmpeg 変換
│   ├── exporters.py   #   TXT / SRT / VTT 出力
│   ├── db.py          #   SQLite 永続化
│   ├── auth.py        #   認証の差し込みポイント (v1 は匿名)
│   └── config.py      #   環境変数ベースの設定
├── web/               # React + Vite フロントエンド
├── Dockerfile         # フロントビルド + アプリの multi-stage build
├── docker-compose.yml # app + caddy
├── docker-compose.gpu.yml
└── Caddyfile
```

## 制約・既知の挙動

- Whisper はストリーミング非対応のため、リアルタイムは「VAD で発話の切れ目を検出 → 区間ごとに推論」する方式です。発話が終わってから 1〜数秒でテキストが確定します
- 発話が 18 秒以上続く場合は途中で強制的に区切って推論します
- アップロードされた元ファイルは文字起こし完了後に削除されます(結果は SQLite に保持)
