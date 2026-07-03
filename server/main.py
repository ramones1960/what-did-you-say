"""FastAPI アプリ本体。API とビルド済みフロントエンドの配信を担う。"""

import logging
import re
import urllib.parse
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
from fastapi import Depends, FastAPI, HTTPException, UploadFile, WebSocket
from fastapi.responses import FileResponse, JSONResponse, Response

from . import auth, config, db, exporters, jobs, realtime

logging.basicConfig(level=logging.INFO)

WEB_DIST = Path(__file__).resolve().parent.parent / "web" / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.ensure_dirs()
    db.init_db()
    jobs.start_workers()
    yield
    await jobs.stop_workers()


app = FastAPI(title="what-did-you-say", lifespan=lifespan)


@app.get("/api/health")
async def health() -> dict:
    return {"status": "ok", "model": config.MODEL_NAME}


@app.post("/api/jobs")
async def create_job(
    file: UploadFile,
    language: str = "",
    user: auth.User = Depends(auth.get_current_user),
) -> dict:
    lang = language.strip() or None
    if lang and not re.fullmatch(r"[a-z]{2,3}", lang):
        raise HTTPException(400, "言語コードが不正です")

    job_id = db.create_job(file.filename or "unnamed", lang)
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
    return db.list_jobs()


@app.get("/api/jobs/{job_id}")
async def get_job(
    job_id: str,
    segments_from: int = 0,
    user: auth.User = Depends(auth.get_current_user),
) -> dict:
    job = db.get_job(job_id)
    if job is None:
        raise HTTPException(404, "ジョブが見つかりません")
    job["segments"] = db.get_segments(job_id, offset=segments_from)
    return job


@app.delete("/api/jobs/{job_id}")
async def remove_job(
    job_id: str, user: auth.User = Depends(auth.get_current_user)
) -> dict:
    job = db.get_job(job_id)
    if job is None:
        raise HTTPException(404, "ジョブが見つかりません")
    if job["status"] in ("queued", "processing"):
        raise HTTPException(409, "処理中のジョブは削除できません")
    jobs.upload_path(job_id).unlink(missing_ok=True)
    db.delete_job(job_id)
    return {"deleted": job_id}


@app.get("/api/jobs/{job_id}/export")
async def export_job(
    job_id: str,
    format: str = "txt",
    user: auth.User = Depends(auth.get_current_user),
) -> Response:
    job = db.get_job(job_id)
    if job is None:
        raise HTTPException(404, "ジョブが見つかりません")
    segments = db.get_segments(job_id)
    if format == "json":
        return JSONResponse({"job": job, "segments": segments})
    if format not in exporters.FORMATS:
        raise HTTPException(400, f"未対応のフォーマットです: {format}")
    render, media_type, ext = exporters.FORMATS[format]
    stem = Path(job["filename"]).stem or "transcript"
    quoted = urllib.parse.quote(f"{stem}.{ext}")
    return Response(
        content=render(segments),
        media_type=media_type,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quoted}"
        },
    )


@app.websocket("/ws/realtime")
async def ws_realtime(ws: WebSocket) -> None:
    await realtime.handle_websocket(ws)


# --- フロントエンド(ビルド済み SPA)の配信 ---

if WEB_DIST.is_dir():
    from fastapi.staticfiles import StaticFiles

    app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")

    @app.get("/{path:path}")
    async def spa(path: str) -> FileResponse:
        target = (WEB_DIST / path).resolve()
        if path and target.is_file() and target.is_relative_to(WEB_DIST):
            return FileResponse(target)
        return FileResponse(WEB_DIST / "index.html")
