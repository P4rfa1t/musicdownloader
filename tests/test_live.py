"""联网集成测试：真实请求四个平台。默认跳过，用 `pytest -m live` 手动跑。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mdl.http import Http  # noqa: E402
from mdl.providers import ALL_PROVIDERS, Registry  # noqa: E402

pytestmark = pytest.mark.live


@pytest.fixture()
async def registry():
    http = Http()
    await http.open()
    try:
        yield Registry(http)
    finally:
        await http.close()


@pytest.mark.parametrize("name", ALL_PROVIDERS)
async def test_search_returns_tracks(registry: Registry, name: str) -> None:
    tracks = await registry.get(name).search("晴天", 5)
    assert tracks, f"{name} 搜索无结果"
    first = tracks[0]
    assert first.title
    assert first.id
    assert first.provider == name


@pytest.mark.parametrize("name", ALL_PROVIDERS)
async def test_track_detail(registry: Registry, name: str) -> None:
    prov = registry.get(name)
    tracks = await prov.search("晴天", 3)
    assert tracks
    detail = await prov.track_from_id(tracks[0].id)
    assert detail.id
    assert detail.title


async def test_netease_stream_downloadable(registry: Registry) -> None:
    prov = registry.get("netease")
    tracks = await prov.search("卡农 钢琴", 5)
    assert tracks
    for track in tracks:
        try:
            stream = await prov.stream(track, "320")
        except Exception:
            continue
        assert stream.url.startswith("http")
        assert stream.quality in {"128", "192", "320", "flac"}
        return
    pytest.skip("当前无免费可下载曲目")


async def test_kugou_stream_downloadable(registry: Registry) -> None:
    prov = registry.get("kugou")
    tracks = await prov.search("Canon", 5)
    assert tracks
    for track in tracks:
        try:
            stream = await prov.stream(track, "320")
        except Exception:
            continue
        assert stream.url.startswith("http")
        return
    pytest.skip("当前无免费可下载曲目")


async def test_resolve_netease_url(registry: Registry) -> None:
    result = await registry.get("netease").resolve_url("https://music.163.com/song?id=5257138")
    assert result.tracks
    assert result.tracks[0].title


async def test_netease_playlist(registry: Registry) -> None:
    result = await registry.get("netease").resolve_url("https://music.163.com/playlist?id=3778678")
    assert result.kind == "playlist"
    assert len(result.tracks) > 10
