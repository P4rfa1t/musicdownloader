"""下载引擎：直链 -> 磁盘，含进度、断点、封面/歌词嵌入、批量任务。"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from .config import settings
from .http import Http, HttpError
from .models import StreamInfo, Track
from .naming import build_dir, build_stem, unique_path
from .providers import Registry
from .providers.base import StreamUnavailable
from .tagger import tag_file

log = logging.getLogger("mdl.downloader")

ProgressFn = Callable[[str, int, int], None]


@dataclass
class DownloadResult:
    ok: bool
    title: str
    provider: str
    path: Path | None = None
    size: int = 0
    quality: str = ""
    error: str = ""
    elapsed: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "title": self.title,
            "provider": self.provider,
            "file": self.path.name if self.path else "",
            "path": _rel(self.path) if self.path else "",
            "size": self.size,
            "quality": self.quality,
            "error": self.error,
            "elapsed": round(self.elapsed, 2),
        }


def _rel(path: Path | None) -> str:
    if path is None:
        return ""
    try:
        return path.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return path.as_posix()


class Downloader:
    """把 Track 变成磁盘上的音频文件。"""

    def __init__(self, registry: Registry, http: Http) -> None:
        self.registry = registry
        self.http = http

    async def download(
        self,
        track: Track,
        quality: str = "320",
        *,
        progress: ProgressFn | None = None,
        index: int = 0,
    ) -> DownloadResult:
        started = time.perf_counter()
        provider = self.registry.get(track.provider)

        # 0) 缺封面/时长时回查一次详情，保证标签完整
        if not track.cover_url or not track.duration_ms:
            try:
                full = await provider.track_from_id(track.id)
                track.cover_url = track.cover_url or full.cover_url
                track.duration_ms = track.duration_ms or full.duration_ms
                track.album = track.album or full.album
                track.artists = track.artists or full.artists
                track.extra = {**full.extra, **track.extra}
            except Exception as exc:
                log.debug("回查详情失败 %s: %s", track.title, exc)

        # 1) 取直链
        try:
            stream = await provider.stream(track, quality)
        except StreamUnavailable as exc:
            return DownloadResult(
                ok=False,
                title=track.title,
                provider=track.provider,
                error=str(exc),
                elapsed=time.perf_counter() - started,
            )
        except (HttpError, ValueError) as exc:
            return DownloadResult(
                ok=False,
                title=track.title,
                provider=track.provider,
                error=f"取直链失败：{exc}",
                elapsed=time.perf_counter() - started,
            )

        # 2) 下载
        directory = build_dir()
        stem = build_stem(track, index)
        target = unique_path(directory, stem, _ext(stream))
        tmp = target.with_suffix(target.suffix + ".part")
        try:
            size = await self._fetch(stream, tmp, progress)
        except Exception as exc:
            tmp.unlink(missing_ok=True)
            return DownloadResult(
                ok=False,
                title=track.title,
                provider=track.provider,
                error=f"下载失败：{exc}",
                elapsed=time.perf_counter() - started,
            )
        if size <= 0:
            tmp.unlink(missing_ok=True)
            return DownloadResult(
                ok=False,
                title=track.title,
                provider=track.provider,
                error="下载到的文件为空",
                elapsed=time.perf_counter() - started,
            )
        # 试听片段检测：远小于按时长估算的体积时判为片段，不落盘
        expected = _expected_bytes(track.duration_ms, stream.bitrate)
        if expected and size < expected * 0.35:
            tmp.unlink(missing_ok=True)
            return DownloadResult(
                ok=False,
                title=track.title,
                provider=track.provider,
                error=(
                    f"只拿到试听片段（{size // 1024} KB，完整约 {expected // 1024 // 1024} MB），"
                    "该曲目需要会员或登录 Cookie"
                ),
                elapsed=time.perf_counter() - started,
            )
        tmp.replace(target)

        # 3) 封面 + 歌词 + 标签
        cover = await self._cover_bytes(track)
        lyric = ""
        if settings.embed_lyric and provider.supports_lyric:
            try:
                lyric = await provider.lyric(track)
            except Exception as exc:
                log.debug("歌词获取失败 %s: %s", track.title, exc)
        if settings.embed_cover or settings.embed_lyric:
            tag_file(target, track, cover=cover, lyric=lyric)
        if settings.save_lyric_file and lyric:
            target.with_suffix(".lrc").write_text(lyric, encoding="utf-8")

        return DownloadResult(
            ok=True,
            title=track.title,
            provider=track.provider,
            path=target,
            size=target.stat().st_size,
            quality=stream.quality,
            elapsed=time.perf_counter() - started,
        )

    async def _fetch(self, stream: StreamInfo, tmp: Path, progress: ProgressFn | None) -> int:
        """流式写盘，边写边报进度。"""
        headers = dict(stream.headers or {})
        headers.setdefault("User-Agent", settings.user_agent)
        tmp.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        total = stream.size or 0
        async with self.http.client.stream("GET", stream.url, headers=headers) as resp:
            if resp.status_code >= 400:
                raise HttpError(f"HTTP {resp.status_code}")
            if not total:
                total = int(resp.headers.get("Content-Length") or 0)
            with tmp.open("wb") as fh:
                async for chunk in resp.aiter_bytes(64 * 1024):
                    if not chunk:
                        continue
                    fh.write(chunk)
                    written += len(chunk)
                    if progress:
                        progress(stream.quality, written, total)
        return written

    async def _cover_bytes(self, track: Track) -> bytes:
        if not settings.embed_cover or not track.cover_url:
            return b""
        try:
            resp = await self.http.request(
                "GET",
                track.cover_url,
                headers={"User-Agent": settings.user_agent, "Referer": self.registry.get(track.provider).referer},
            )
            if resp.status_code == 200 and len(resp.content) > 1024:
                return resp.content
        except Exception as exc:
            log.debug("封面下载失败 %s: %s", track.title, exc)
        return b""


def _ext(stream: StreamInfo) -> str:
    ext = (stream.ext or "mp3").lower().lstrip(".")
    return {"mpeg": "mp3", "mp4": "m4a", "ogg": "ogg"}.get(ext, ext)


def _expected_bytes(duration_ms: int, bitrate: int) -> int:
    """按时长与码率估算完整文件字节数；信息不全时返回 0（不校验）。"""
    if duration_ms <= 0 or bitrate <= 0:
        return 0
    return int(duration_ms / 1000 * bitrate / 8)


# ---------------------------------------------------------------- 批量任务


@dataclass
class JobItem:
    title: str
    provider: str
    state: str = "pending"  # pending / running / done / failed
    file: str = ""
    path: str = ""
    size: int = 0
    quality: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "provider": self.provider,
            "state": self.state,
            "file": self.file,
            "path": self.path,
            "size": self.size,
            "quality": self.quality,
            "error": self.error,
        }


@dataclass
class Job:
    id: str
    total: int
    items: list[JobItem] = field(default_factory=list)
    state: str = "running"
    finished: int = 0
    ok: int = 0
    failed: int = 0
    current: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "state": self.state,
            "total": self.total,
            "finished": self.finished,
            "ok": self.ok,
            "failed": self.failed,
            "current": self.current,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "items": [i.to_dict() for i in self.items],
        }


class JobManager:
    """内存任务表，最多保留 50 个任务。"""

    def __init__(self, downloader: Downloader) -> None:
        self.downloader = downloader
        self.jobs: dict[str, Job] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def create(self, tracks: list[Track], quality: str = "320") -> Job:
        job = Job(
            id=uuid.uuid4().hex[:8],
            total=len(tracks),
            items=[JobItem(title=t.title, provider=t.provider) for t in tracks],
        )
        self.jobs[job.id] = job
        self._prune()
        self._tasks[job.id] = asyncio.create_task(self._run(job, tracks, quality))
        return job

    async def _run(self, job: Job, tracks: list[Track], quality: str) -> None:
        sem = asyncio.Semaphore(max(1, settings.concurrency))

        async def one(idx: int, track: Track) -> None:
            item = job.items[idx]
            async with sem:
                item.state = "running"
                job.current = f"正在下载 {track.title}"
                job.updated_at = time.time()
                result = await self.downloader.download(track, quality, index=idx + 1)
                job.finished += 1
                job.updated_at = time.time()
                if result.ok:
                    item.state = "done"
                    item.file = result.path.name if result.path else ""
                    item.path = _rel(result.path)
                    item.size = result.size
                    item.quality = result.quality
                    job.ok += 1
                else:
                    item.state = "failed"
                    item.error = result.error
                    job.failed += 1

        try:
            await asyncio.gather(*(one(i, t) for i, t in enumerate(tracks)))
        except Exception as exc:  # 任务级异常也要落状态
            log.exception("批量任务 %s 异常", job.id)
            job.state = "failed"
            job.current = str(exc)
        else:
            job.state = "done"
            job.current = ""
        finally:
            job.updated_at = time.time()
            self._tasks.pop(job.id, None)

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    def _prune(self) -> None:
        if len(self.jobs) <= 50:
            return
        for jid in sorted(self.jobs, key=lambda j: self.jobs[j].created_at)[: len(self.jobs) - 50]:
            if self.jobs[jid].state != "running":
                self.jobs.pop(jid, None)

    async def shutdown(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()
