"""在线播放：直链解析 + 短期令牌 + 带 Range 的流式代理。

为什么要代理而不是把 CDN 直链丢给 <audio>：

1. 直链常带防盗链（Referer / Cookie），浏览器发的是本站 Referer，命中不了；
2. 直链有有效期（网易云 URL 里带时间戳），播放中途重取会拿到新 URL，
   而 <audio> 的 src 已经固定，靠代理才能透明换链；
3. 部分 CDN 不带 CORS 头，代理成同源后拖动进度条才不会报错。

代理由此提供 Range 透传，<audio> 的 seek 才可用。
"""

from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from fastapi.responses import Response, StreamingResponse

from .http import Http, HttpError
from .models import StreamInfo, Track
from .providers import Registry
from .providers.base import StreamUnavailable

log = logging.getLogger("mdl.player")

# 令牌能用多久：一次播放会话内足够，过期后前端重新 prepare 即可
TOKEN_TTL = 1800.0
# 直链复用窗口：同一首歌重复播放 / 拖动进度条时不必反复打平台接口
STREAM_TTL = 240.0
# 同时保留的令牌上限，防止长跑进程里无限增长
MAX_TOKENS = 128


@dataclass
class PlayEntry:
    """一次「可播放」的准备结果。"""

    token: str
    track: Track
    quality: str
    stream: StreamInfo
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "token": self.token,
            "url": f"/api/play/{self.token}",
            "provider": self.track.provider,
            "id": self.track.id,
            "title": self.track.title,
            "artists": self.track.artists,
            "album": self.track.album,
            "cover_url": self.track.cover_url,
            "duration_ms": self.track.duration_ms,
            "quality": self.stream.quality or self.quality,
            "bitrate": self.stream.bitrate,
            "size": self.stream.size,
            "ext": self.stream.ext,
        }


@dataclass
class _Cached:
    stream: StreamInfo
    at: float


class PlayService:
    """管理播放令牌与直链缓存。"""

    def __init__(self, registry: Registry, http: Http) -> None:
        self._registry = registry
        self._http = http
        self._entries: dict[str, PlayEntry] = {}
        self._streams: dict[tuple[str, str, str], _Cached] = {}

    # ---------- 准备 ----------

    async def prepare(self, track: Track, quality: str = "320") -> PlayEntry:
        stream = await self._stream_for(track, quality)
        self._prune()
        token = secrets.token_urlsafe(12)
        entry = PlayEntry(token=token, track=track, quality=quality, stream=stream)
        self._entries[token] = entry
        return entry

    async def _stream_for(self, track: Track, quality: str) -> StreamInfo:
        key = (track.provider, track.id, quality)
        hit = self._streams.get(key)
        if hit and time.time() - hit.at < STREAM_TTL:
            return hit.stream
        provider = self._registry.get(track.provider)
        stream = await provider.stream(track, quality)
        self._streams[key] = _Cached(stream=stream, at=time.time())
        return stream

    # ---------- 取令牌 ----------

    def get(self, token: str) -> PlayEntry | None:
        entry = self._entries.get(token)
        if entry is None:
            return None
        if time.time() - entry.created_at > TOKEN_TTL:
            self._entries.pop(token, None)
            return None
        return entry

    def drop(self, token: str) -> None:
        self._entries.pop(token, None)

    def _prune(self) -> None:
        now = time.time()
        for tok in [t for t, e in self._entries.items() if now - e.created_at > TOKEN_TTL]:
            self._entries.pop(tok, None)
        if len(self._entries) > MAX_TOKENS:
            # 令牌是按时间追加的，先扔最老的
            for tok, _ in sorted(self._entries.items(), key=lambda kv: kv[1].created_at)[
                : len(self._entries) - MAX_TOKENS
            ]:
                self._entries.pop(tok, None)
        for k in [k for k, c in self._streams.items() if now - c.at > STREAM_TTL * 4]:
            self._streams.pop(k, None)


async def proxy(
    http: Http,
    entry: PlayEntry,
    range_header: str = "",
    method: str = "GET",
) -> Response:
    """把上游音频流转给浏览器，透传 Range / Content-Range。"""
    headers = {k: v for k, v in (entry.stream.headers or {}).items() if k.lower() != "cookie"}
    # 上游要的是「按字节区间给」，浏览器要的是完整语义，两边头不能混用
    if range_header:
        headers["Range"] = range_header
    headers.setdefault("Accept", "*/*")

    try:
        client = http.client
        request = client.build_request(method, entry.stream.url, headers=headers)
        resp = await client.send(request, stream=True)
    except HttpError as exc:
        log.warning("播放取流失败 %s: %s", entry.track.title, exc)
        return Response(status_code=502, content=f"上游取流失败: {exc}".encode())
    except Exception as exc:  # noqa: BLE001 - httpx 的各种传输异常
        log.warning("播放取流异常 %s: %s", entry.track.title, exc)
        return Response(status_code=502, content=f"上游取流失败: {exc}".encode())

    if resp.status_code >= 400:
        detail = f"上游返回 HTTP {resp.status_code}"
        await resp.aclose()
        # 416 要带 Content-Range 告诉浏览器合法区间，否则它会一直重试
        if resp.status_code == 416:
            return Response(
                status_code=416,
                headers={"Content-Range": resp.headers.get("content-range", "bytes */*")},
            )
        log.warning("播放上游 %s: %s", detail, entry.stream.url)
        # 直链过期是最常见的 4xx 原因，清缓存让下次 prepare 重新解析
        return Response(status_code=502, content=detail.encode())

    out: dict[str, str] = {"Accept-Ranges": "bytes", "Cache-Control": "no-store"}
    ctype = resp.headers.get("content-type") or _guess_type(entry.stream.ext)
    for src, dst in (
        ("content-length", "Content-Length"),
        ("content-range", "Content-Range"),
    ):
        value = resp.headers.get(src)
        if value:
            out[dst] = value

    async def body():
        try:
            async for chunk in resp.aiter_bytes(64 * 1024):
                yield chunk
        finally:
            await resp.aclose()

    return StreamingResponse(body(), status_code=resp.status_code, headers=out, media_type=ctype)


def _guess_type(ext: str) -> str:
    return {
        "mp3": "audio/mpeg",
        "flac": "audio/flac",
        "m4a": "audio/mp4",
        "aac": "audio/aac",
        "ogg": "audio/ogg",
        "wav": "audio/wav",
    }.get((ext or "").lower(), "application/octet-stream")


__all__ = ["PlayService", "PlayEntry", "proxy", "StreamUnavailable", "TOKEN_TTL"]