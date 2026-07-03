# アーキテクチャ・機能仕様

このドキュメントは、あとからメンテナンスする人(または AI)が全体像を素早く掴むためのものです。使い方・セットアップは [README.md](../README.md) を参照してください。

## 1. 全体像

```
[ブラウザ (React SPA)]
   │  https (REST) / wss (WebSocket)
   ▼
[Caddy]  ← TLS 終端。getUserMedia が localhost 以外で HTTPS 必須のため
   │  reverse_proxy
   ▼
[FastAPI (server/)]
   ├─ main.py       REST API・WebSocket・SPA 配信のルーティング
   ├─ realtime.py   リアルタイム文字起こし (WebSocket + VAD)
   ├─ jobs.py       ファイル文字起こしのジョブキュー + ワーカー
   ├─ transcriber.py faster-whisper (Whisper) のラッパー
   ├─ diarize.py    話者分離 (sherpa-onnx 話者埋め込み + クラスタリング)
   ├─ media.py      ffmpeg / ffprobe (動画・音声 → 16kHz mono WAV)
   ├─ exporters.py  TXT / SRT / VTT 生成 (話者氏名の反映を含む)
   ├─ db.py         SQLite 永続化 (jobs / segments / presets)
   ├─ auth.py       認証の差し込みポイント (v1 は匿名スタブ)
   └─ config.py     環境変数ベースの設定
```

- **完全ローカル**: 外部通信は「初回のモデルダウンロード(Hugging Face)」のみ。音声・結果は外部に出ない
- **デプロイ単位**: Docker Compose(`app` + `caddy` の2サービス)。WSL2 でも社内サーバーでも同一手順

## 2. 主要な処理フロー

### 2.1 リアルタイム文字起こし

```
マイク → AudioWorklet(web/public/pcm-worklet.js)
        ブラウザの 44.1k/48kHz Float32 を線形補間で 16kHz int16 に変換
      → WebSocket /ws/realtime にバイナリ送信 (128ms ごと)
      → サーバー: バッファに蓄積 → 0.5秒ごとに Silero VAD 実行
        「発話終了(末尾 0.6 秒無音)」を検出したらそこまでを faster-whisper で推論
      → {"type":"segment", start, end, text, speaker} を返す
```

- Whisper はストリーミング非対応のため「VAD で発話単位に区切ってチャンク推論」する設計。**発話が終わってから1〜数秒でテキスト確定**という体験になる
- 18秒以上発話が続いたら途中でも強制推論(`MAX_BUFFER_SECONDS`)
- 一時停止(pause)はそこまでのバッファを確定。再開(resume)時にクライアントが中断実時間 `gap` を渡し、サーバーが時刻オフセットに加算 → **時刻タグは録音開始からの実時間を維持**
- WebSocket メッセージ仕様の正典は `server/realtime.py` のモジュール docstring
- 同時セッション数は `MAX_REALTIME_SESSIONS`(既定2)で制限

### 2.2 ファイル文字起こし

```
POST /api/jobs (multipart) → jobs テーブルに queued で登録、即 ID 返却
  → asyncio.Queue → ワーカー (JOB_WORKERS 個)
     → ffmpeg で 16kHz mono WAV 化 (動画は音声抽出)
     → faster-whisper (vad_filter=True) でセグメント逐次生成
       → 1セグメント確定するたび segments テーブルへ INSERT + progress 更新
  → クライアントは GET /api/jobs/{id}?segments_from=N を1.5秒ポーリング
    (取得済み idx 以降の差分だけ受け取る)
```

- 元ファイルは完了時に削除し、結果(セグメント)だけを DB に残す
- ジョブ状態: `queued → processing → done / error`
- キューはインプロセスのためプロセス再起動で消える(DB 上は queued のまま残る)。スケール時は `jobs.py` を Redis + 別プロセスワーカーに差し替える

### 2.3 話者分離(ダイアライゼーション)

- セグメントの音声から **話者埋め込み**(声紋ベクトル・192次元)を sherpa-onnx + CAM++ モデルで抽出
- **オンラインクラスタリング**: セッション内の既存話者セントロイドとコサイン類似度を取り、`SPEAKER_THRESHOLD`(既定0.4)以上なら同一話者としてセントロイド更新、未満なら新話者
- 0.5秒未満の短いセグメントは判定せず直前の話者を継承
- ラベルは「話者1, 話者2, …」の**セッション内連番(仮名)**。氏名は `jobs.speaker_names`(JSON)に別途保存し、表示・エクスポート時にマッピングする(**元データは仮名のまま**なので後から何度でも付け替え可能)
- モデル取得失敗・`DIARIZATION=0` のときは speaker が NULL になり、他機能は影響を受けない

### 2.4 発言の区切り(セグメント結合)

Whisper / VAD の認識セグメントは細かくなりがちなので、**認識は細かい粒度のまま保存し、表示・TXT 出力時に結合する**(非破壊。後から何度でも粒度を変えられる)。

- UI: 結果画面の「発言の区切り」セレクタ(短い / 標準 / 長い)。選択は localStorage に保存
- 結合ルール: 「同一話者」かつ「間隔が gap 未満」かつ「結合後が最大長・最大文字数以内」の連続セグメントを結合。時刻タグは結合ブロック先頭の時刻
- パラメータ(gap/最大長/最大文字数): 短い=結合なし、標準=1.5s/30s/120字、長い=4s/60s/240字。定義は `web/src/lib.ts` と `server/exporters.py` の2箇所にあり、**変更時は両方を揃える**
- 適用範囲: 画面表示とリアルタイムの TXT 保存はクライアント側で、ファイルの TXT エクスポートはサーバー側(`?granularity=`)で結合。SRT / VTT は字幕用途のため常に細かい粒度、JSON は生データ

### 2.5 認識精度向上(用語リスト・コンテキスト)

- **用語リスト** → faster-whisper の `hotwords`(全ウィンドウに効く)
- **コンテキスト** → `initial_prompt`(冒頭の文脈・文体)
- どちらも「バイアス」であり確実な置換ではない。プロンプト実効長は約224トークンのため入力は1000文字に制限(`MAX_PROMPT_CHARS`)
- **プリセット**: 名前付きの用語リスト+コンテキストの組を presets テーブルに保存。全利用者で共有。同名保存は上書き

## 3. データベース(SQLite)

ファイル: `$DATA_DIR/app.db`(Docker では `app_data` ボリューム)。スキーマの正典は `server/db.py` の `_SCHEMA`。

| テーブル | 用途 | 主なカラム |
|---|---|---|
| jobs | ファイル文字起こしのジョブ | id, filename, status, error, language, vocabulary, context, speaker_names(JSON), duration, progress, created_at |
| segments | 文字起こし結果 | job_id, idx, start, end, text, speaker(1始まり/NULL) |
| presets | 用語リスト等の共有プリセット | id, name(UNIQUE), vocabulary, context, updated_at |

マイグレーションは `init_db()` 内の `ALTER TABLE ... ADD COLUMN`(既存なら無視)方式。カラム追加時はここに追記する。

## 4. HTTP API

一覧は `server/main.py` のモジュール docstring 参照。特記事項:

- 全エンドポイントが `Depends(auth.get_current_user)` を通る。**認証を導入する場合は `server/auth.py` の1関数を差し替えるだけ**
- `GET /api/jobs/{id}?segments_from=N` は差分取得(ポーリング効率化)
- エクスポートのファイル名は RFC 5987(`filename*=UTF-8''...`)で日本語対応

## 5. フロントエンド(web/)

React 18 + Vite + TypeScript。ビルド成果物(`web/dist`)を FastAPI が配信する。開発時は Vite dev サーバーが `/api` `/ws` を :8000 にプロキシ(`vite.config.ts`)。

| ファイル | 役割 |
|---|---|
| src/App.tsx | ルート。タブは両方マウントしたまま hidden で切替(状態保持のため) |
| src/Recorder.tsx | リアルタイム画面。状態遷移・WebSocket は冒頭のコメント参照 |
| src/Upload.tsx | ファイル画面。アップロード・差分ポーリング・エクスポート |
| src/PromptSettings.tsx | 用語リスト・コンテキスト入力 + プリセット管理。値は localStorage、プリセットはサーバー |
| src/SpeakerNames.tsx | 話者仮名 → 氏名の一括振り分けパネル |
| src/lib.ts | 型定義と共有ユーティリティ(時刻整形・話者ラベル等) |
| public/pcm-worklet.js | AudioWorklet。マイク音声を 16kHz int16 PCM に変換 |

UI 文言は日本語。デザインは `src/styles.css` に集約(業務アプリ調、ダークモード対応)。

## 6. 設定(環境変数)

正典は `server/config.py`。主要なもの:

| 変数 | 既定 | 意味 |
|---|---|---|
| WHISPER_MODEL | small | Whisper モデル名(tiny/base/small/medium/large-v3) |
| WHISPER_DEVICE / WHISPER_COMPUTE_TYPE | auto | cuda + float16 で GPU 利用 |
| JOB_WORKERS | 1 | ファイル文字起こしの並列数 |
| MAX_REALTIME_SESSIONS | 2 | リアルタイム同時接続上限 |
| DIARIZATION | 1 | 話者分離の有効/無効 |
| SPEAKER_THRESHOLD | 0.4 | 話者クラスタリングのしきい値 |
| DATA_DIR | ./data | アップロード先・SQLite の場所 |
| HTTP(S)_PROXY / NO_PROXY | - | モデルダウンロード・ビルドに伝搬 |

## 7. 同時実行の設計

- **モデル推論は `transcriber.inference_lock` で全体1本に直列化**(CPU/GPU の取り合い防止)。並列度を上げたい場合はワーカープロセス分離が安全
- 話者埋め込みの抽出も `diarize._lock` で直列化
- ブロッキング処理(推論・ffmpeg・埋め込み)は必ず `asyncio.to_thread` 経由でイベントループの外で実行する
- SQLite は WAL モード + 操作ごとの短い接続で十分な規模

## 8. 社内公開時の変更ポイント

1. `SITE_ADDRESS` をホスト名に変更、証明書を社内 CA に(`Caddyfile`)
2. `server/auth.py` の `get_current_user` を OIDC / LDAP 等に差し替え
3. `docker-compose.yml` の app の `ports:`(8000 直結)を削除して Caddy 経由のみに
4. 利用者数に応じて `JOB_WORKERS` / `MAX_REALTIME_SESSIONS` を調整
5. スケールが必要なら `jobs.py` を Redis キュー + 別プロセスワーカーへ

## 9. 既知の制約

- リアルタイムは発話終了後に確定する方式(逐字表示ではない)。字幕的な逐次表示が必要なら「暫定(partial)表示」の追加が次の拡張候補
- 話者分離はベストエフォート。声質が近い話者は同一視されうる。会議録音(遠いマイク・被り)では精度が落ちる
- 用語リストは認識バイアスであり確実な置換ではない。表記の強制統一は「文字起こし後の置換辞書」(未実装)で対応する想定
- キューはインプロセス(再起動で消える)。queued のまま残ったジョブの自動再投入は未実装
