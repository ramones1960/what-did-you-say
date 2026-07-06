"""FastAPI アプリ本体。REST API・WebSocket・ビルド済みフロントエンドの配信を担う。

エンドポイント一覧(詳細は docs/architecture.md):
  GET    /api/health                    稼働確認とモデル名
  POST   /api/jobs                      ファイルアップロード → ジョブ作成
  GET    /api/jobs                      ジョブ一覧(新しい順)
  GET    /api/jobs/{id}                 ジョブ詳細 + セグメント(ポーリング用)
  DELETE /api/jobs/{id}                 ジョブ削除
  PUT    /api/jobs/{id}/speakers        話者番号→氏名マッピングの保存
  GET    /api/jobs/{id}/export          TXT / SRT / VTT / JSON エクスポート
  POST   /api/jobs/{id}/summaries       LLM で要約 / 議事録を生成して保存(llm.py)
  POST   /api/summarize                 セグメントを直接渡して LLM 生成(リアルタイム用・保存なし)
  GET    /api/presets                   用語リスト・コンテキストのプリセット一覧
  POST   /api/presets                   プリセット作成(同名は上書き)
  DELETE /api/presets/{id}              プリセット削除
  WS     /ws/realtime                   リアルタイム文字起こし(realtime.py)
  GET    /{path}                        SPA 配信(web/dist、フォールバックは index.html)

全 API は auth.get_current_user を通る(v1 は常に匿名ユーザーを返すスタブ)。
"""

import asyncio
import json
import logging
import re
import urllib.parse
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
from fastapi import Body, Depends, FastAPI, Form, HTTPException, UploadFile, WebSocket
from fastapi.responses import FileResponse, JSONResponse, Response

from . import auth, config, db, exporters, jobs, llm, realtime

logging.basicConfig(level=logging.INFO)

WEB_DIST = Path(__file__).resolve().parent.parent / "web" / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """起動時にディレクトリ・DB・ワーカーを準備し、終了時にワーカーを止める。"""
    config.ensure_dirs()
    db.init_db()
    jobs.start_workers()
    yield
    await jobs.stop_workers()


app = FastAPI(title="what-did-you-say", lifespan=lifespan)


@app.get("/api/health")
async def health() -> dict:
    """稼働確認。フロントがモデル名表示と LLM 機能(要約・議事録)の出し分けに使う。"""
    return {
        "status": "ok",
        "model": config.MODEL_NAME,
        "llm": {
            "enabled": llm.enabled(),
            "model": config.LLM_MODEL if llm.enabled() else None,
        },
    }


# 用語リスト・コンテキストの入力上限(文字数)。Whisper のプロンプトは
# 約224トークンしか効かないため、これ以上長くしても効果がない
MAX_PROMPT_CHARS = 1000


@app.post("/api/jobs")
async def create_job(
    file: UploadFile,
    language: str = "",
    vocabulary: str = Form(""),
    context: str = Form(""),
    user: auth.User = Depends(auth.get_current_user),
) -> dict:
    """ファイルを受け取ってジョブを作成し、キューに積んで即座に ID を返す。

    処理自体は非同期(jobs.py のワーカー)で行われるため、クライアントは
    返された ID で GET /api/jobs/{id} をポーリングして進捗を追う。
    language はクエリ、vocabulary / context はフォーム項目で受け取る。
    """
    lang = language.strip() or None
    if lang and not re.fullmatch(r"[a-z]{2,3}", lang):
        raise HTTPException(400, "言語コードが不正です")
    if len(vocabulary) > MAX_PROMPT_CHARS or len(context) > MAX_PROMPT_CHARS:
        raise HTTPException(400, f"用語リスト・コンテキストは{MAX_PROMPT_CHARS}文字以内にしてください")

    job_id = db.create_job(
        file.filename or "unnamed",
        lang,
        vocabulary=vocabulary.strip() or None,
        context=context.strip() or None,
    )
    dest = jobs.upload_path(job_id)
    size = 0
    async with await anyio.open_file(dest, "wb") as f:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > config.MAX_UPLOAD_BYTES:
                await f.aclose()
                dest.unlink(missing_ok=True)
                db.delete_job(job_id)
                raise HTTPException(413, "ファイルサイズが上限を超えています")
            await f.write(chunk)
    if size == 0:
        dest.unlink(missing_ok=True)
        db.delete_job(job_id)
        raise HTTPException(400, "空のファイルです")

    await jobs.enqueue(job_id)
    return {"id": job_id}


@app.get("/api/jobs")
async def get_jobs(user: auth.User = Depends(auth.get_current_user)) -> list:
    """ジョブ一覧(新しい順、セグメントは含まない)。"""
    return db.list_jobs()


@app.get("/api/jobs/{job_id}")
async def get_job(
    job_id: str,
    segments_from: int = 0,
    user: auth.User = Depends(auth.get_current_user),
) -> dict:
    """ジョブ詳細とセグメントを返す。

    segments_from に前回取得済みのセグメント数を渡すと差分だけが返るため、
    処理中のポーリングで全件を再取得せずに済む(web/src/Upload.tsx 参照)。
    """
    job = db.get_job(job_id)
    if job is None:
        raise HTTPException(404, "ジョブが見つかりません")
    job["segments"] = db.get_segments(job_id, offset=segments_from)
    job["summaries"] = db.get_summaries(job_id)
    return job


@app.delete("/api/jobs/{job_id}")
async def remove_job(
    job_id: str, user: auth.User = Depends(auth.get_current_user)
) -> dict:
    """完了・エラーのジョブを結果ごと削除する(処理中は 409)。"""
    job = db.get_job(job_id)
    if job is None:
        raise HTTPException(404, "ジョブが見つかりません")
    if job["status"] in ("queued", "processing"):
        raise HTTPException(409, "処理中のジョブは削除できません")
    jobs.upload_path(job_id).unlink(missing_ok=True)
    db.delete_job(job_id)
    return {"deleted": job_id}


@app.put("/api/jobs/{job_id}/speakers")
async def set_speakers(
    job_id: str,
    names: dict[str, str] = Body(embed=True),
    user: auth.User = Depends(auth.get_current_user),
) -> dict:
    """話者番号→氏名のマッピングを保存する(エクスポートに反映される)。"""
    if db.get_job(job_id) is None:
        raise HTTPException(404, "ジョブが見つかりません")
    cleaned = {}
    for key, value in names.items():
        if not re.fullmatch(r"\d{1,3}", key) or len(value) > 100:
            raise HTTPException(400, "話者マッピングが不正です")
        if value.strip():
            cleaned[key] = value.strip()
    db.set_speaker_names(job_id, json.dumps(cleaned, ensure_ascii=False))
    return {"speaker_names": cleaned}


@app.get("/api/jobs/{job_id}/export")
async def export_job(
    job_id: str,
    format: str = "txt",
    granularity: str = "standard",
    user: auth.User = Depends(auth.get_current_user),
) -> Response:
    """文字起こし結果を format (txt/srt/vtt/json) 指定でダウンロードさせる。

    話者に氏名が登録されていれば「氏名: テキスト」の形で反映される。
    granularity (short/standard/long) は「発言の区切り」で、TXT のみに
    適用される(SRT/VTT は字幕用途のため常に細かい粒度、JSON は生データ)。
    ファイル名は元ファイル名 + 拡張子(日本語名は RFC 5987 でエンコード)。
    """
    job = db.get_job(job_id)
    if job is None:
        raise HTTPException(404, "ジョブが見つかりません")
    segments = db.get_segments(job_id)
    if format == "json":
        return JSONResponse({"job": job, "segments": segments})
    if format not in exporters.FORMATS:
        raise HTTPException(400, f"未対応のフォーマットです: {format}")
    if format == "txt":
        segments = exporters.merge_segments(segments, granularity)
    try:
        names = json.loads(job.get("speaker_names") or "{}")
    except json.JSONDecodeError:
        names = {}
    render, media_type, ext = exporters.FORMATS[format]
    stem = Path(job["filename"]).stem or "transcript"
    quoted = urllib.parse.quote(f"{stem}.{ext}")
    return Response(
        content=render(segments, names),
        media_type=media_type,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quoted}"
        },
    )


# --- LLM 連携(要約・議事録) ---

# リアルタイム画面から直接渡せるセグメント数の上限(異常な巨大リクエスト対策)
MAX_SUMMARIZE_SEGMENTS = 20000


def _validate_summarize_segments(segments: list) -> list[dict]:
    """クライアントから直接渡されたセグメント一覧を検証・整形する。"""
    if not isinstance(segments, list) or not segments:
        raise HTTPException(400, "セグメントがありません")
    if len(segments) > MAX_SUMMARIZE_SEGMENTS:
        raise HTTPException(400, "セグメント数が多すぎます")
    cleaned = []
    for seg in segments:
        try:
            cleaned.append(
                {
                    "start": float(seg["start"]),
                    "end": float(seg["end"]),
                    "text": str(seg["text"]),
                    "speaker": seg.get("speaker"),
                }
            )
        except (TypeError, KeyError, ValueError):
            raise HTTPException(400, "セグメントの形式が不正です")
    return cleaned


@app.post("/api/jobs/{job_id}/summaries")
async def create_job_summary(
    job_id: str,
    kind: str = Body(embed=True),
    user: auth.User = Depends(auth.get_current_user),
) -> dict:
    """完了済みジョブの文字起こしから要約 / 議事録を LLM で生成して保存する。

    kind は summary(要約)/ minutes(議事録)。同じ種類は再生成で上書き。
    生成には数十秒〜数分かかる(ローカル LLM の性能次第)。
    """
    if not llm.enabled():
        raise HTTPException(503, "LLM 連携が設定されていません(LLM_API_URL を設定してください)")
    if kind not in llm.KINDS:
        raise HTTPException(400, f"未対応の生成種類です: {kind}")
    job = db.get_job(job_id)
    if job is None:
        raise HTTPException(404, "ジョブが見つかりません")
    if job["status"] != "done":
        raise HTTPException(409, "文字起こしが完了してから生成してください")
    segments = db.get_segments(job_id)
    if not segments:
        raise HTTPException(400, "文字起こし結果が空のため生成できません")
    try:
        names = json.loads(job.get("speaker_names") or "{}")
    except json.JSONDecodeError:
        names = {}
    try:
        content = await asyncio.to_thread(llm.generate, kind, segments, names)
    except llm.LLMError as e:
        raise HTTPException(502, str(e))
    return db.save_summary(job_id, kind, content, config.LLM_MODEL)


@app.post("/api/summarize")
async def summarize(
    kind: str = Body(embed=True),
    segments: list = Body(embed=True),
    speaker_names: dict[str, str] = Body(default={}, embed=True),
    user: auth.User = Depends(auth.get_current_user),
) -> dict:
    """セグメントを直接渡して要約 / 議事録を生成する(保存しない)。

    リアルタイム文字起こしの結果はサーバーに保存されないため、
    クライアントが持っているセグメントをそのまま送ってもらう。
    """
    if not llm.enabled():
        raise HTTPException(503, "LLM 連携が設定されていません(LLM_API_URL を設定してください)")
    if kind not in llm.KINDS:
        raise HTTPException(400, f"未対応の生成種類です: {kind}")
    cleaned = _validate_summarize_segments(segments)
    names = {k: v for k, v in speaker_names.items() if isinstance(v, str)}
    try:
        content = await asyncio.to_thread(llm.generate, kind, cleaned, names)
    except llm.LLMError as e:
        raise HTTPException(502, str(e))
    return {"kind": kind, "content": content, "model": config.LLM_MODEL}


# --- 用語リスト・コンテキストのプリセット ---

@app.get("/api/presets")
async def list_presets(user: auth.User = Depends(auth.get_current_user)) -> list:
    """プリセット一覧(名前順)。全利用者で共有される。"""
    return db.list_presets()


@app.post("/api/presets")
async def save_preset(
    name: str = Body(...),
    vocabulary: str = Body(""),
    context: str = Body(""),
    user: auth.User = Depends(auth.get_current_user),
) -> dict:
    """プリセットを保存する。同名が存在すれば上書き(ID は維持)。"""
    name = name.strip()
    if not name or len(name) > 100:
        raise HTTPException(400, "プリセット名は1〜100文字で入力してください")
    if len(vocabulary) > MAX_PROMPT_CHARS or len(context) > MAX_PROMPT_CHARS:
        raise HTTPException(400, f"用語リスト・コンテキストは{MAX_PROMPT_CHARS}文字以内にしてください")
    return db.save_preset(name, vocabulary, context)


@app.delete("/api/presets/{preset_id}")
async def remove_preset(
    preset_id: str, user: auth.User = Depends(auth.get_current_user)
) -> dict:
    if not db.delete_preset(preset_id):
        raise HTTPException(404, "プリセットが見つかりません")
    return {"deleted": preset_id}


@app.websocket("/ws/realtime")
async def ws_realtime(ws: WebSocket) -> None:
    """リアルタイム文字起こし。プロトコルは realtime.py のモジュール docstring 参照。"""
    await realtime.handle_websocket(ws)


# --- フロントエンド(ビルド済み SPA)の配信 ---

if WEB_DIST.is_dir():
    from fastapi.staticfiles import StaticFiles

    app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")

    @app.get("/{path:path}")
    async def spa(path: str) -> FileResponse:
        """SPA 配信。実在するファイルはそのまま、それ以外は index.html を返す。

        resolve() + is_relative_to() でパストラバーサルを防いでいる。
        """
        target = (WEB_DIST / path).resolve()
        if path and target.is_file() and target.is_relative_to(WEB_DIST):
            return FileResponse(target)
        return FileResponse(WEB_DIST / "index.html")
