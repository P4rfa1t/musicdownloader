"""FastAPI 应用：搜索 / 解析 / 下载 / 批量任务 / 静态页面。"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__
from .config import settings
from .downloader import Downloader, JobManager
from .http import Http, HttpError
from .models import QUALITIES, Track
from .providers import ALL_PROVIDERS, Registry
from .providers.base import StreamUnavailable

log = logging.getLogger("mdl.server")

WEB_DIR = Path(__file__).parent / "web"

# 这些异常从 provider 抛出来时统一转 400/502
PROVIDER_ERRORS = (ValueError, HttpError, StreamUnavailable)


class AppState:
    http: Http
    registry: Registry
    downloader: Downloader
    jobs: JobManager


state = AppState()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    http = Http(settings.cookies)
    await http.open()
    state.http = http
    state.registry = Registry(http)
    state.downloader = Downloader(state.registry, http)
    state.jobs = JobManager(state.downloader)
    log.info("MusicDL 就绪，下载目录 %s", Path(settings.download_dir).resolve())
    try:
        yield
    finally:
        await state.jobs.shutdown()
        await http.close()


app = FastAPI(title="MusicDL", version=__version__, lifespan=lifespan)


# ------------------------------------------------------------------ 请求体


class ResolveBody(BaseModel):
    url: str = Field(..., min_length=1, description="分享链接、分享文案或纯 id")


class TrackBody(BaseModel):
    provider: str
    id: str
    title: str = ""
    artists: list[str] = Field(default_factory=list)
    album: str = ""
    duration_ms: int = 0
    cover_url: str = ""
    source_url: str = ""
    extra: dict[str, Any] = Field(default_factory=dict)

    def to_track(self) -> Track:
        return Track(
            provider=self.provider,
            id=self.id,
            title=self.title or f"{self.provider}-{self.id}",
            artists=list(self.artists),
            album=self.album,
            duration_ms=self.duration_ms,
            cover_url=self.cover_url,
            source_url=self.source_url,
            extra=dict(self.extra),
        )


class DownloadBody(TrackBody):
    quality: str = "320"


class BatchBody(BaseModel):
    quality: str = "320"
    tracks: list[TrackBody] = Field(..., min_length=1, max_length=200)


# ------------------------------------------------------------------ 基础接口


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "version": __version__,
        "providers": state.registry.names() if hasattr(state, "registry") else ALL_PROVIDERS,
        "download_dir": str(Path(settings.download_dir).resolve()),
    }


@app.get("/api/qualities")
async def qualities() -> dict[str, Any]:
    return {"qualities": QUALITIES, "default": "320"}


# ------------------------------------------------------------------ 搜索


@app.get("/api/search")
async def search(
    q: str = Query(..., min_length=1, description="关键词"),
    provider: str = Query("all", description="all 或具体平台名"),
    limit: int = Query(20, ge=1, le=50),
) -> dict[str, Any]:
    targets = state.registry.names() if provider == "all" else [provider]
    for name in targets:
        if name not in state.registry.providers:
            raise HTTPException(status_code=400, detail=f"不支持的平台: {name}")

    groups: list[dict[str, Any]] = []
    for name in targets:
        prov = state.registry.get(name)
        try:
            tracks = await prov.search(q, limit)
            groups.append(
                {
                    "provider": name,
                    "ok": True,
                    "error": "",
                    "tracks": [t.to_dict() for t in tracks],
                }
            )
        except PROVIDER_ERRORS as exc:
            log.warning("搜索失败 %s: %s", name, exc)
            groups.append({"provider": name, "ok": False, "error": str(exc), "tracks": []})
        except Exception as exc:  # 单个平台炸掉不影响其他平台
            log.exception("搜索异常 %s", name)
            groups.append(
                {"provider": name, "ok": False, "error": f"内部错误: {exc}", "tracks": []}
            )
    return {"query": q, "groups": groups}


# ------------------------------------------------------------------ 解析


@app.post("/api/resolve")
async def resolve(body: ResolveBody) -> dict[str, Any]:
    try:
        target = state.registry.detect(body.url)
        provider = state.registry.get(target.provider)
        result = await provider.resolve_url(body.url)
    except PROVIDER_ERRORS as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        log.exception("解析失败")
        raise HTTPException(status_code=502, detail=f"解析失败: {exc}") from exc
    payload = result.to_dict()
    payload["target"] = target.to_dict()
    return payload


# ------------------------------------------------------------------ 下载


def _check_quality(quality: str) -> str:
    if quality not in QUALITIES:
        raise HTTPException(status_code=400, detail=f"音质必须是 {QUALITIES} 之一")
    return quality


@app.post("/api/download")
async def download(body: DownloadBody) -> dict[str, Any]:
    _check_quality(body.quality)
    if body.provider not in state.registry.providers:
        raise HTTPException(status_code=400, detail=f"不支持的平台: {body.provider}")
    track = body.to_track()
    if not track.duration_ms or not track.cover_url:
        # 前端传来的可能是搜索结果，字段已够；缺失时才回查详情
        try:
            full = await state.registry.get(track.provider).track_from_id(track.id)
            track.duration_ms = track.duration_ms or full.duration_ms
            track.cover_url = track.cover_url or full.cover_url
            track.album = track.album or full.album
            track.artists = track.artists or full.artists
            track.extra = {**full.extra, **track.extra}
        except Exception:
            pass

    result = await state.downloader.download(track, body.quality)
    if not result.ok:
        raise HTTPException(status_code=502, detail=result.error)
    return result.to_dict()


@app.post("/api/download/batch")
async def download_batch(body: BatchBody) -> dict[str, Any]:
    _check_quality(body.quality)
    tracks: list[Track] = []
    for tb in body.tracks:
        if tb.provider not in state.registry.providers:
            raise HTTPException(status_code=400, detail=f"不支持的平台: {tb.provider}")
        tracks.append(tb.to_track())
    job = state.jobs.create(tracks, body.quality)
    return {"job_id": job.id, "total": job.total}


@app.get("/api/jobs/{job_id}")
async def job_detail(job_id: str) -> dict[str, Any]:
    job = state.jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    return job.to_dict()


@app.get("/api/jobs")
async def job_list() -> dict[str, Any]:
    jobs = sorted(state.jobs.jobs.values(), key=lambda j: j.created_at, reverse=True)
    return {"jobs": [j.to_dict() for j in jobs[:20]]}


# ------------------------------------------------------------------ 本地文件


@app.get("/api/file")
async def local_file(path: str = Query(..., description="下载目录内的相对路径")) -> FileResponse:
    root = Path(settings.download_dir).resolve()
    try:
        target = (root / path).resolve()
    except (OSError, ValueError):
        raise HTTPException(status_code=400, detail="路径非法") from None
    if not str(target).startswith(str(root)):
        raise HTTPException(status_code=403, detail="只能访问下载目录内的文件")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(target, filename=target.name)


# ------------------------------------------------------------------ 静态页面


@app.exception_handler(StreamUnavailable)
async def _stream_unavailable(_request, exc: StreamUnavailable) -> JSONResponse:
    return JSONResponse(status_code=502, content={"detail": str(exc)})


if WEB_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")


def run(host: str = "127.0.0.1", port: int = 8000, reload: bool = False) -> None:
    import uvicorn

    uvicorn.run("mdl.server:app" if reload else app, host=host, port=port, reload=reload)
