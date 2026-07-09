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
   ├─ llm.py        ローカル LLM 連携 (要約・議事録の生成、任意機能)
   ├─ db.py         SQLite 永続化 (jobs / segments / presets / summaries)
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
- **暫定(partial)表示**: 発話が続いている間も `REALTIME_PARTIAL_INTERVAL`(既定2秒)ごとに未確定バッファを推論し、`{"type":"partial", start, text}` を送る。確定ではないため次の partial か segment で置き換えられ、保存対象にはならない。推論ロック使用中はスキップするベストエフォート(確定側・他セッション優先)。`0` で無効化
- 18秒以上発話が続いたら途中でも強制推論(`MAX_BUFFER_SECONDS`)
- 一時停止(pause)はそこまでのバッファを確定。再開(resume)時にクライアントが中断実時間 `gap` を渡し、サーバーが時刻オフセットに加算 → **時刻タグは録音開始からの実時間を維持**
- WebSocket メッセージ仕様の正典は `server/realtime.py` のモジュール docstring
- 同時セッション数は `MAX_REALTIME_SESSIONS`(既定2)で制限

### 2.2 ファイル文字起こし

```
ファイル選択 / D&D → 「選択中」として保留(まだ送らない)
  → 言語・用語リスト等を設定して「文字起こしを開始」
POST /api/jobs (multipart) → jobs テーブルに queued で登録、即 ID 返却
  → asyncio.Queue → ワーカー (JOB_WORKERS 個)
     → ffmpeg で 16kHz mono WAV 化 (動画は音声抽出)
     → faster-whisper (vad_filter=True) でセグメント逐次生成
       → 1セグメント確定するたび segments テーブルへ INSERT + progress 更新
  → クライアントは GET /api/jobs/{id}?segments_from=N を1.5秒ポーリング
    (取得済み idx 以降の差分だけ受け取る)
```

- アップロードは**即時開始しない**。ファイルを選ぶと「選択中」として保留し、言語・用語リストを整えてから開始ボタンで送る(`web/src/Upload.tsx`)
- 元ファイルは完了時に削除し、結果(セグメント)だけを DB に残す
- ジョブ状態: `queued → processing → done / error / canceled`
- **中断(キャンセル)**: `POST /api/jobs/{id}/cancel` で待機中・処理中のジョブを打ち切れる。待機中はその場で `canceled` にし、処理中は `jobs.request_cancel()` がプロセス内メモリにフラグを立て、ワーカーが**次のセグメント境界**で `JobCanceled` を送出して止める。Whisper はセグメント境界でしか止められないため、通常の会話音声(無音でセグメントが細かく区切れる)なら数秒で止まるが、無音が少なく1セグメントが長い音声ではそのセグメントの推論が終わるまで遅れる。**途中まで確定したセグメントは残す**(非破壊。確認・エクスポート可能)。元ファイルはワーカーが片付ける
- キューはインプロセスのためプロセス再起動で消えるが、**起動時に `jobs.requeue_stale_jobs()` が DB 上に queued / processing のまま残ったジョブを回収して再投入する**(元ファイルが残っていれば途中結果を破棄して最初からやり直し、無ければ error)。スケール時は `jobs.py` を Redis + 別プロセスワーカーに差し替える

### 2.3 話者分離(ダイアライゼーション)

2段構え。処理中は逐次(オンライン)割り当てで暫定表示し、ファイル文字起こしは完了時に一括(オフライン)処理で置き換えて確定する(`server/diarize.py`)。

- **逐次割り当て**(リアルタイム・ファイル処理中の暫定): セグメントの音声から **話者埋め込み**(声紋ベクトル・192次元)を sherpa-onnx + CAM++ モデルで抽出し、セッション内の既存話者セントロイドとコサイン類似度を取り、`SPEAKER_THRESHOLD`(既定0.4)以上なら同一話者としてセントロイド更新、未満なら新話者。0.5秒未満の短いセグメントは判定せず直前の話者を継承
- **一括話者分離**(ファイル文字起こしの完了時): sherpa-onnx の OfflineSpeakerDiarization(pyannote segmentation-3.0 の ONNX 版 + 同じ話者埋め込みモデル)で音声全体を解析し、話者区間と Whisper セグメントの時間重なりで話者を割り当て直す。逐次と違い処理順に依存せず、話者交代の検出も行うため精度が高い。アップロード時に**話者の人数**を指定するとクラスタ数として固定され(人数既知ならさらに頑健)、未指定なら `DIARIZATION_CLUSTER_THRESHOLD`(既定0.5)で自動推定。モデル未取得・失敗時は逐次割り当ての結果のまま完了する(非致命)。中断されたジョブは一括処理を行わない
- ラベルは「話者1, 話者2, …」の**セッション内連番(仮名)**(一括処理後も登場順に振り直す)。氏名は `jobs.speaker_names`(JSON)に別途保存し、表示・エクスポート時にマッピングする(**元データは仮名のまま**なので後から何度でも付け替え可能)
- モデル取得失敗・`DIARIZATION=0` のときは speaker が NULL になり、他機能は影響を受けない

### 2.4 発言の区切り(セグメント結合)

Whisper / VAD の認識セグメントは細かくなりがちなので、**認識は細かい粒度のまま保存し、表示・TXT 出力時に結合する**(非破壊。後から何度でも粒度を変えられる)。

- UI: 結果画面の「発言の区切り」セレクタ(短い / 標準 / 長い)。選択は localStorage に保存
- 結合ルール: 「同一話者」かつ「間隔が gap 未満」かつ「結合後が最大長・最大文字数以内」の連続セグメントを結合。時刻タグは結合ブロック先頭の時刻
- パラメータ(gap/最大長/最大文字数): 短い=結合なし、標準=1.5s/30s/120字、長い=4s/60s/240字。**正典は `shared/merge_params.json` の1箇所**で、`web/src/lib.ts` と `server/exporters.py` の両方がこのファイルを読む(値の変更は JSON だけでよい。結合ロジック自体を変えるときは両実装を揃える)
- 適用範囲: 画面表示とリアルタイムの TXT 保存はクライアント側で、ファイルの TXT エクスポートはサーバー側(`?granularity=`)で結合。SRT / VTT は字幕用途のため常に細かい粒度、JSON は生データ

### 2.5 認識精度向上(用語リスト・コンテキスト)

- **用語リスト** → faster-whisper の `hotwords`(全ウィンドウに効く)
- **コンテキスト** → `initial_prompt`(冒頭の文脈・文体)
- どちらも「バイアス」であり確実な置換ではない。プロンプト実効長は約224トークンのため入力は1000文字に制限(`MAX_PROMPT_CHARS`)
- **プリセット**: 名前付きの用語リスト+コンテキストの組を presets テーブルに保存。全利用者で共有。同名保存は上書き
- **頻出単語の洗い出し**: 文字起こし結果から繰り返し出てくる語を出現回数つきで一覧する(`GET /api/jobs/{id}/words`、集計は `server/wordfreq.py`)。誤認識されている語を用語リストに足して同じファイルを再アップロードすると精度が上がる、という手動ループを支援する。外部辞書・重い依存を足さない方針のため、形態素解析ではなく正規表現で「用語になりやすいトークン(2文字以上の漢字語・カタカナ語・英数字語)」だけを抽出する軽量方式。ひらがな主体の機能語は拾わない

### 2.6 要約・議事録の生成(ローカル LLM 連携・任意機能)

```
POST /api/jobs/{id}/summaries {kind}   ← ファイル文字起こし(結果は summaries テーブルに保存)
POST /api/summarize {kind, segments}   ← リアルタイム(クライアントのセグメントを直接渡す・保存なし)
  → llm.build_transcript: 「長い」粒度で結合 + 話者氏名反映 + LLM_MAX_INPUT_CHARS で切り詰め
  → llm.generate: OpenAI 互換 API ({LLM_API_URL}/chat/completions) に投げる
```

- **`LLM_API_URL` が未設定なら機能ごと無効**。`/api/health` の `llm.enabled` でフロントが UI を出し分ける。「完全ローカル」の前提を守るため、既定ではどこにも接続しない。Ollama / LM Studio / llama.cpp server などローカルの OpenAI 互換サーバーを指定する想定(外部 SaaS を指定しないこと)
- kind は `summary`(要約)/ `minutes`(議事録)。プロンプトの正典は `server/llm.py` の `KINDS`
- ジョブ紐付けの生成結果は summaries テーブルに保存され、同じ kind の再生成で上書き。ジョブ詳細(GET /api/jobs/{id})に `summaries` として同梱される
- 生成はブロッキング(urllib)なので `asyncio.to_thread` 経由。推論の inference_lock とは無関係(LLM は別プロセス/別サーバー)

## 3. データベース(SQLite)

ファイル: `$DATA_DIR/app.db`(Docker では `app_data` ボリューム)。スキーマの正典は `server/db.py` の `_SCHEMA`。

| テーブル | 用途 | 主なカラム |
|---|---|---|
| jobs | ファイル文字起こしのジョブ | id, filename, status, error, language, vocabulary, context, speaker_names(JSON), num_speakers, duration, progress, created_at |
| segments | 文字起こし結果 | job_id, idx, start, end, text, speaker(1始まり/NULL) |
| presets | 用語リスト等の共有プリセット | id, name(UNIQUE), vocabulary, context, updated_at |
| summaries | LLM 生成の要約・議事録 | job_id, kind(summary/minutes), content, model, created_at(job_id×kind で1件、再生成は上書き) |

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
| src/SummaryPanel.tsx | 要約・議事録の生成パネル(両画面共通、LLM 無効時は非表示) |
| src/lib.ts | 型定義と共有ユーティリティ(時刻整形・話者ラベル等) |
| public/pcm-worklet.js | AudioWorklet。マイク音声を 16kHz int16 PCM に変換 |

UI 文言は日本語。デザインは `src/styles.css` に集約(業務アプリ調、ダークモード対応)。

## 6. 設定(環境変数)

正典は `server/config.py`。主要なもの:

| 変数 | 既定 | 意味 |
|---|---|---|
| WHISPER_MODEL | small | Whisper モデル名(tiny/base/small/medium/large-v3) |
| WHISPER_DEVICE / WHISPER_COMPUTE_TYPE | auto | cuda + float16 で GPU 利用(VRAM 不足時は int8_float16) |
| WHISPER_BEAM_SIZE | 5 | デコードの探索ビーム幅。大きいほど精度が上がりうるが推論は遅くなる |
| JOB_WORKERS | 1 | ファイル文字起こしの並列数 |
| MAX_REALTIME_SESSIONS | 2 | リアルタイム同時接続上限 |
| REALTIME_PARTIAL_INTERVAL | 2.0 | リアルタイム暫定表示の間隔(秒)。0 で無効化 |
| DIARIZATION | 1 | 話者分離の有効/無効 |
| SPEAKER_THRESHOLD | 0.4 | 逐次割り当てのコサイン類似度しきい値 |
| DIARIZATION_CLUSTER_THRESHOLD | 0.5 | 一括話者分離のクラスタリングしきい値(人数未指定時の自動推定。上げると話者がまとまりやすい) |
| LLM_API_URL | (空=無効) | ローカル LLM の OpenAI 互換 API ベース URL(例: http://localhost:11434/v1) |
| LLM_MODEL | qwen2.5:7b-instruct | 要約・議事録に使う LLM モデル名 |
| LLM_TIMEOUT / LLM_MAX_INPUT_CHARS | 300 / 24000 | LLM 生成の待ち時間上限(秒)/ 入力文字数上限 |
| DATA_DIR | ./data | アップロード先・SQLite の場所 |
| HTTP(S)_PROXY / NO_PROXY | - | モデルダウンロード・ビルドに伝搬 |

## 7. 同時実行の設計

- **モデル推論は `transcriber.inference_lock` で全体1本に直列化**(CPU/GPU の取り合い防止)。並列度を上げたい場合はワーカープロセス分離が安全
- 話者埋め込みの抽出は `diarize._lock`、一括話者分離は `diarize._offline_lock` で直列化(一括処理は長時間かかるため、リアルタイムの埋め込み抽出をブロックしないよう別ロック)
- ブロッキング処理(推論・ffmpeg・埋め込み)は必ず `asyncio.to_thread` 経由でイベントループの外で実行する
- SQLite は WAL モード + 操作ごとの短い接続で十分な規模

## 8. 社内公開時の変更ポイント

1. `SITE_ADDRESS` をホスト名に変更、証明書を社内 CA に(`Caddyfile`)
2. `server/auth.py` の `get_current_user` を OIDC / LDAP 等に差し替え
3. `docker-compose.yml` の app の `ports:`(8000 直結)を削除して Caddy 経由のみに
4. 利用者数に応じて `JOB_WORKERS` / `MAX_REALTIME_SESSIONS` を調整
5. スケールが必要なら `jobs.py` を Redis キュー + 別プロセスワーカーへ

## 9. テスト

`tests/` に pytest スイートがある(モデル推論・モデルダウンロードは行わない。環境設定は `tests/conftest.py`)。

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests/
```

- 対象: exporters(結合・整形)/ db(マイグレーション含む)/ jobs(再起動リカバリ)/ API(バリデーション)/ llm(入力整形・エラー変換)
- CI: `.github/workflows/ci.yml` が push / PR ごとに pytest とフロントのビルド(型チェック)を回す
- 推論を含む end-to-end の確認は従来どおり手動(curl / Playwright、`WHISPER_MODEL=tiny` 推奨)

## 10. 既知の制約

- リアルタイムの暫定(partial)表示はベストエフォート(推論が混んでいるときはスキップされる)。確定は従来どおり発話終了後
- 話者分離はベストエフォート。声質が近い話者は同一視されうる。会議録音(遠いマイク・被り)では精度が落ちる。リアルタイムは逐次割り当てのみで、一括処理による確定はファイル文字起こしだけ
- 用語リストは認識バイアスであり確実な置換ではない。表記の強制統一は「文字起こし後の置換辞書」(未実装)で対応する想定
- 再起動時のジョブ再投入は「最初からやり直し」方式(Whisper は途中再開できないため、processing 途中のセグメントは破棄される)
- 要約・議事録の品質はローカル LLM のモデル性能に依存する。長い会議は LLM_MAX_INPUT_CHARS で切り詰められる(分割要約は未実装)
