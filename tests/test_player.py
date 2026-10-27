"""播放链路测试：令牌准备、同源代理、Range 透传、过期与失败分支。

上游用一块内存里的假 CDN（ASGI app + httpx ASGITransport），不碰真实网络。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import Response
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mdl.models import StreamInfo, Track  # noqa: E402
from mdl.player import PlayService  # noqa: E402
from mdl.providers.base import StreamUnavailable  # noqa: E402
from mdl.server import app, heartbeat, state  # noqa: E402

AUDIO = bytes(range(64))  # 64 字节，够验证区间切片


# ------------------------------------------------------------------ 假上游 CDN


async def _cdn(request: Request):
    """最小 Range 实现：够用来确认代理有没有把区间透传过去。"""
    data = AUDIO
    rng = request.headers.get("range") or ""
    if not rng.startswith("bytes="):
        return Response(content=data, media_type="audio/mpeg",
                        headers={"Content-Length": str(len(data)), "Accept-Ranges": "bytes"})
    spec = rng.split("=", 1)[1]
    start_s, _, end_s = spec.partition("-")
    if start_s == "":
        return Response(status_code=416, headers={"Content-Range": f"bytes */{len(data)}"})
    start = int(start_s)
    end = int(end_s) if end_s else len(data) - 1
    end = min(end, len(data) - 1)
    if start >= len(data):
        return Response(status_code=416, headers={"Content-Range": f"bytes */{len(data)}"})
    chunk = data[start : end + 1]
    return Response(
        content=chunk,
        status_code=206,
        media_type="audio/mpeg",
        headers={
            "Content-Range": f"bytes {start}-{end}/{len(data)}",
            "Content-Length": str(len(chunk)),
            "Accept-Ranges": "bytes",
        },
    )


cdn_app = FastAPI()
cdn_app.add_api_route("/x.mp3", _cdn, methods=["GET"])
cdn_app.add_api_route("/broken.mp3", lambda: Response(status_code=404), methods=["GET"])


class FakeHttp:
    """只需要 .client 供代理层用。"""

    def __init__(self, client: AsyncClient) -> None:
        self._client = client

    @property
    def client(self) -> AsyncClient:
        return self._client


# ------------------------------------------------------------------ 假 provider


class StubProvider:
    name = "netease"
    referer = "https://music.163.com/"

    def __init__(self) -> None:
        self.stream_calls = 0
        self.track = Track(
            provider="netease",
            id="5257138",
            title="屋顶",
            artists=["周杰伦", "温岚"],
            album="叶惠美",
            duration_ms=319000,
            cover_url="https://p3.music.126.net/x.jpg",
        )

    async def search(self, keyword: str, limit: int = 20) -> list[Track]:
        return [self.track]

    async def track_from_id(self, track_id: str) -> Track:
        return self.track

    async def stream(self, track: Track, quality: str = "320") -> StreamInfo:
        self.stream_calls += 1
        if quality == "flac":
            raise StreamUnavailable(self.name, track, "该曲目没有无损")
        return StreamInfo(
            url="http://cdn/x.mp3",
            ext="mp3",
            bitrate=320000,
            size=len(AUDIO),
            quality="320",
            headers={"Referer": "https://music.163.com/", "Cookie": "MUSIC_U=secret"},
        )


class StubRegistry:
    def __init__(self, provider: StubProvider) -> None:
        self.providers: dict[str, Any] = {"netease": provider}

    def get(self, name: str) -> Any:
        if name not in self.providers:
            raise ValueError(f"不支持的平台: {name}")
        return self.providers[name]

    def names(self) -> list[str]:
        return list(self.providers)


@pytest.fixture()
async def env(monkeypatch: pytest.MonkeyPatch):
    provider = StubProvider()
    registry = StubRegistry(provider)
    upstream = AsyncClient(transport=ASGITransport(app=cdn_app), base_url="http://cdn")
    http = FakeHttp(upstream)

    monkeypatch.setattr(state, "registry", registry, raising=False)
    monkeypatch.setattr(state, "http", http, raising=False)
    monkeypatch.setattr(state, "play", PlayService(registry, http), raising=False)

    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    async with client as c:
        yield c, provider
    await upstream.aclose()


PAYLOAD = {
    "provider": "netease",
    "id": "5257138",
    "title": "屋顶",
    "artists": ["周杰伦"],
    "quality": "320",
}


# ------------------------------------------------------------------ 用例


async def test_prepare_returns_token(env) -> None:
    client, provider = env
    r = await client.post("/api/play/prepare", json=PAYLOAD)
    body = r.json()
    assert r.status_code == 200, body
    assert body["token"]
    assert body["url"] == f"/api/play/{body['token']}"
    assert body["quality"] == "320"
    assert body["size"] == len(AUDIO)
    assert body["title"] == "屋顶"
    assert provider.stream_calls == 1


async def test_prepare_reuses_cached_stream(env) -> None:
    client, provider = env
    await client.post("/api/play/prepare", json=PAYLOAD)
    await client.post("/api/play/prepare", json=PAYLOAD)
    # 直链有 TTL，重复播放不该反复打平台接口
    assert provider.stream_calls == 1


async def test_play_proxies_full_body(env) -> None:
    client, _ = env
    token = (await client.post("/api/play/prepare", json=PAYLOAD)).json()["token"]
    r = await client.get(f"/api/play/{token}")
    assert r.status_code == 200
    assert r.content == AUDIO
    assert r.headers["content-type"].startswith("audio/mpeg")
    assert r.headers["accept-ranges"] == "bytes"


async def test_play_passes_range_through(env) -> None:
    client, _ = env
    token = (await client.post("/api/play/prepare", json=PAYLOAD)).json()["token"]
    r = await client.get(f"/api/play/{token}", headers={"Range": "bytes=2-5"})
    assert r.status_code == 206
    assert r.content == AUDIO[2:6]
    assert r.headers["content-range"] == f"bytes 2-5/{len(AUDIO)}"


async def test_play_strips_cookie_header(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """代理不该把账号 Cookie 转手发给第三方 CDN。"""
    seen: dict[str, str] = {}

    async def spy(request: Request):
        seen.update({k.lower(): v for k, v in request.headers.items()})
        return await _cdn(request)

    cdn_app.add_api_route("/spy.mp3", spy, methods=["GET"])
    client, provider = env
    r = await client.post("/api/play/prepare", json=PAYLOAD)
    token = r.json()["token"]

    entry = state.play.get(token)
    entry.stream.url = "http://cdn/spy.mp3"
    await client.get(f"/api/play/{token}")
    assert "cookie" not in seen
    assert seen.get("referer") == "https://music.163.com/"


async def test_play_unknown_token(env) -> None:
    client, _ = env
    r = await client.get("/api/play/nope")
    assert r.status_code == 404
    assert "过期" in r.json()["detail"]


async def test_prepare_stream_unavailable(env) -> None:
    client, _ = env
    payload = dict(PAYLOAD, quality="flac")
    r = await client.post("/api/play/prepare", json=payload)
    assert r.status_code == 502
    assert "无损" in r.json()["detail"]


async def test_prepare_bad_quality(env) -> None:
    client, _ = env
    r = await client.post("/api/play/prepare", json=dict(PAYLOAD, quality="999"))
    assert r.status_code == 400


async def test_prepare_unknown_provider(env) -> None:
    client, _ = env
    r = await client.post("/api/play/prepare", json=dict(PAYLOAD, provider="spotify"))
    assert r.status_code == 400


async def test_play_upstream_error_is_502(env) -> None:
    client, _ = env
    token = (await client.post("/api/play/prepare", json=PAYLOAD)).json()["token"]
    entry = state.play.get(token)
    entry.stream.url = "http://cdn/broken.mp3"
    r = await client.get(f"/api/play/{token}")
    assert r.status_code == 502
    assert "404" in r.text


async def test_token_expires(env, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = env
    token = (await client.post("/api/play/prepare", json=PAYLOAD)).json()["token"]
    entry = state.play.get(token)
    entry.created_at -= 10_000  # 时间旅行到 TTL 之外
    assert state.play.get(token) is None
    r = await client.get(f"/api/play/{token}")
    assert r.status_code == 404


async def test_ping_marks_heartbeat(env) -> None:
    client, _ = env
    heartbeat._last = 0.0  # noqa: SLF001 - 测试里直接重置内部状态
    assert heartbeat.never_pinged() is True
    r = await client.get("/api/ping")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert heartbeat.alive() is True
    assert heartbeat.idle() < 1.0