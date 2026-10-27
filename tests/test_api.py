"""API 层测试：用 httpx ASGI transport 打真实路由，网络调用用桩替换。"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mdl.downloader import DownloadResult  # noqa: E402
from mdl.models import ResolveResult, StreamInfo, Track  # noqa: E402
from mdl.providers.base import StreamUnavailable  # noqa: E402
from mdl.server import app, state  # noqa: E402


class StubProvider:
    """替换真实 provider，隔离网络。"""

    name = "netease"
    referer = "https://music.163.com/"
    supports_lyric = True

    def __init__(self) -> None:
        self.search_result: list[Track] = [
            Track(
                provider="netease",
                id="5257138",
                title="屋顶",
                artists=["周杰伦", "温岚"],
                album="叶惠美",
                duration_ms=319000,
                cover_url="https://p3.music.126.net/x.jpg",
            )
        ]
        self.raise_on_stream: Exception | None = None

    async def search(self, keyword: str, limit: int = 20) -> list[Track]:
        if keyword == "炸掉":
            raise RuntimeError("boom")
        return self.search_result[:limit]

    async def track_from_id(self, track_id: str) -> Track:
        for t in self.search_result:
            if t.id == track_id:
                return t
        raise ValueError(f"不存在: {track_id}")

    async def stream(self, track: Track, quality: str = "320") -> StreamInfo:
        if self.raise_on_stream:
            raise self.raise_on_stream
        return StreamInfo(url="https://cdn/x.mp3", ext="mp3", bitrate=320000, size=1000, quality="320")

    async def lyric(self, track: Track) -> str:
        return "[00:00.00] 歌词"

    def parse_id(self, url: str) -> str | None:
        return "5257138" if "5257138" in url else None

    async def resolve_url(self, url: str) -> ResolveResult:
        track = self.search_result[0]
        return ResolveResult(
            title=track.title,
            tracks=[track],
            provider=self.name,
            cover_url=track.cover_url,
            source_url=url,
        )


class StubRegistry:
    def __init__(self) -> None:
        self.providers: dict[str, Any] = {"netease": StubProvider()}

    def get(self, name: str) -> Any:
        if name not in self.providers:
            raise ValueError(f"不支持的平台: {name}")
        return self.providers[name]

    def names(self) -> list[str]:
        return list(self.providers)

    def detect(self, text: str):
        from mdl.models import Target

        return Target(provider="netease", kind="song", id="5257138", raw_url=text)


class StubDownloader:
    def __init__(self) -> None:
        self.fail = False

    async def download(self, track: Track, quality: str = "320", **kw: Any) -> DownloadResult:
        if self.fail:
            return DownloadResult(
                ok=False, title=track.title, provider=track.provider, error="没有直链"
            )
        return DownloadResult(
            ok=True,
            title=track.title,
            provider=track.provider,
            path=Path("downloads") / f"{track.title}.mp3",
            size=12345,
            quality=quality,
        )


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch):
    stub_registry = StubRegistry()
    stub_downloader = StubDownloader()
    monkeypatch.setattr(state, "registry", stub_registry, raising=False)
    monkeypatch.setattr(state, "downloader", stub_downloader, raising=False)
    # lifespan 会覆盖 state，这里直接跳过启动
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


async def test_health(client: AsyncClient) -> None:
    async with client as c:
        r = await c.get("/api/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


async def test_qualities(client: AsyncClient) -> None:
    async with client as c:
        r = await c.get("/api/qualities")
    assert r.json()["default"] == "320"
    assert "flac" in r.json()["qualities"]


async def test_search_ok(client: AsyncClient) -> None:
    async with client as c:
        r = await c.get("/api/search", params={"q": "屋顶"})
    body = r.json()
    assert r.status_code == 200
    assert body["query"] == "屋顶"
    group = body["groups"][0]
    assert group["ok"] is True
    assert group["tracks"][0]["title"] == "屋顶"


async def test_search_group_error_isolated(client: AsyncClient) -> None:
    async with client as c:
        r = await c.get("/api/search", params={"q": "炸掉"})
    group = r.json()["groups"][0]
    assert group["ok"] is False
    assert "boom" in group["error"]
    assert group["tracks"] == []


async def test_search_bad_provider(client: AsyncClient) -> None:
    async with client as c:
        r = await c.get("/api/search", params={"q": "x", "provider": "spotify"})
    assert r.status_code == 400


async def test_resolve_ok(client: AsyncClient) -> None:
    async with client as c:
        r = await c.post("/api/resolve", json={"url": "https://music.163.com/song?id=5257138"})
    body = r.json()
    assert r.status_code == 200
    assert body["count"] == 1
    assert body["tracks"][0]["id"] == "5257138"


async def test_resolve_empty_url(client: AsyncClient) -> None:
    async with client as c:
        r = await c.post("/api/resolve", json={"url": ""})
    assert r.status_code == 422


async def test_download_ok(client: AsyncClient) -> None:
    payload = {
        "provider": "netease",
        "id": "5257138",
        "title": "屋顶",
        "artists": ["周杰伦"],
        "quality": "320",
    }
    async with client as c:
        r = await c.post("/api/download", json=payload)
    body = r.json()
    assert r.status_code == 200
    assert body["ok"] is True
    assert body["quality"] == "320"


async def test_download_bad_quality(client: AsyncClient) -> None:
    payload = {"provider": "netease", "id": "1", "title": "x", "quality": "999"}
    async with client as c:
        r = await c.post("/api/download", json=payload)
    assert r.status_code == 400


async def test_download_failure_maps_to_502(client: AsyncClient) -> None:
    state.downloader.fail = True  # type: ignore[attr-defined]
    payload = {"provider": "netease", "id": "1", "title": "x", "quality": "320"}
    async with client as c:
        r = await c.post("/api/download", json=payload)
    assert r.status_code == 502
    assert "没有直链" in r.json()["detail"]


async def test_file_traversal_blocked(client: AsyncClient) -> None:
    async with client as c:
        r = await c.get("/api/file", params={"path": "../../../windows/win.ini"})
    assert r.status_code in (403, 404)
