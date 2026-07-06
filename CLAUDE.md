# what-did-you-say 開発ガイド(AI・人間メンテナ向け)

完全ローカルで動く時刻タグ付き文字起こし Web アプリ。
全体像・処理フロー・DB スキーマ・API は **docs/architecture.md** を最初に読むこと。

## コマンド

```bash
# バックエンド (要: python3.11+, ffmpeg)
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn server.main:app --port 8000        # --reload は開発時のみ

# フロントエンド
cd web && npm install
npm run build        # tsc + vite build → web/dist (FastAPI が配信)
npm run dev          # 開発サーバー (:5173, /api と /ws を :8000 にプロキシ)

# Docker (本番相当)
docker compose up -d --build
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build  # GPU
```

```bash
# テスト (モデル推論・ダウンロードなし、数秒で終わる。CI でも実行される)
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests/
```

推論を含む end-to-end の動作確認は「curl で API を叩く」「Playwright(インストール済みの
/opt/pw-browsers/chromium)で UI を操作する」方式で行っている。軽い確認は
`WHISPER_MODEL=tiny` を使うと速い(精度は低いがパイプライン検証には十分)。

## 変更時の注意

- **uvicorn は自動リロードしない**(--reload なし運用)。サーバーコードを変えたら再起動。
  フロントは `npm run build` しないと FastAPI 配信分(web/dist)に反映されない
- **DB スキーマ変更**は `server/db.py` の `_SCHEMA` と `init_db()` のマイグレーション
  (ALTER TABLE)の両方に追記する。既存 DB を壊さないこと
- **WebSocket プロトコル**を変えるときは `server/realtime.py` のモジュール docstring
  (正典)と `web/src/Recorder.tsx` の両方を更新する
- **発言の区切りの結合パラメータ**の正典は `shared/merge_params.json`(サーバー・
  フロントの両方が読む)。値の変更はこのファイルだけでよいが、結合ロジック自体を
  変えるときは `server/exporters.py` と `web/src/lib.ts` の両方を揃える
- **ブロッキング処理**(推論・ffmpeg・埋め込み抽出)は必ず `asyncio.to_thread` 経由。
  イベントループ上で直接呼ぶとリアルタイム音声の受信が詰まる
- **モデル推論は inference_lock で直列化**されている。並列化したい場合は
  ロック除去ではなくワーカープロセス分離を検討する
- タブ UI は両方マウントしたまま hidden で切替(状態保持のため)。
  アンマウント前提のコードを書かない
- UI 文言・コメント・コミットメッセージは日本語

## 設計上の前提(壊さないこと)

- **完全ローカル**: 外部通信は初回のモデルダウンロードのみ。解析系 SaaS や CDN を足さない。
  LLM 連携(`server/llm.py`)もローカルの OpenAI 互換サーバー限定で、未設定なら機能ごと無効
- **認証は auth.py の1関数に集約**: 全 API が `Depends(auth.get_current_user)` を通る。
  エンドポイント追加時も必ず付ける
- **話者ラベルは仮名(話者N)で保存し、氏名は speaker_names マッピングで後付け**。
  segments.speaker に氏名を直接書かない(付け替え可能にするため)
- **getUserMedia は localhost 以外で HTTPS 必須**。マイク系の機能は Caddy(TLS)経由の
  アクセスを前提に考える。フロントの接続先は相対パス + スキーム自動判定を維持
- 用語リスト・コンテキストは 1000 文字上限(Whisper プロンプト実効長 ≈224 トークン)

## ディレクトリの入口

- API ルーティング: `server/main.py`(エンドポイント一覧は docstring)
- リアルタイム処理: `server/realtime.py`
- ジョブ処理: `server/jobs.py`
- 画面: `web/src/App.tsx` → Recorder.tsx / Upload.tsx
- 設定一覧: `server/config.py` と `.env.example`
