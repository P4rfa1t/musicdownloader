"""离线单元测试：链接识别、命名、模型序列化、quality 选择。

这些测试不碰网络，可随时跑。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mdl.models import StreamInfo, Track  # noqa: E402
from mdl.naming import build_stem, sanitize, unique_path  # noqa: E402
from mdl.providers import Registry  # noqa: E402
from mdl.providers.base import pick_quality  # noqa: E402


class FakeHttp:
    """Registry 只需要 headers_for / cookies，这里给个最小替身。"""

    def headers_for(self, provider: str, referer: str = "") -> dict[str, str]:
        return {"Referer": referer} if referer else {}


@pytest.fixture()
def registry() -> Registry:
    return Registry(FakeHttp())  # type: ignore[arg-type]


# ---------------------------------------------------------------- 链接识别


@pytest.mark.parametrize(
    "url,provider,track_id",
    [
        ("https://music.163.com/song?id=5257138", "netease", "5257138"),
        ("https://music.163.com/#/song?id=5257138", "netease", "5257138"),
        ("https://y.music.163.com/m/song?id=5257138", "netease", "5257138"),
        ("https://y.qq.com/n/ryqq/songDetail/0039MnYb0qxYhV", "qq", "0039MnYb0qxYhV"),
        ("https://i.y.qq.com/v8/playsong.html?songmid=0039MnYb0qxYhV", "qq", "0039MnYb0qxYhV"),
        ("https://www.kugou.com/song/#hash=B3A52A7A958BF0AED0EBFBA2E9A818B7", "kugou", "b3a52a7a958bf0aed0ebfba2e9a818b7"),
        ("https://www.kuwo.cn/play_detail/228908", "kuwo", "228908"),
    ],
)
def test_detect_url(registry: Registry, url: str, provider: str, track_id: str) -> None:
    target = registry.detect(url)
    assert target.provider == provider
    assert target.id == track_id


def test_detect_share_text(registry: Registry) -> None:
    text = "分享周杰伦的单曲《晴天》: https://music.163.com/song?id=186016 (来自@网易云音乐)"
    target = registry.detect(text)
    assert target.provider == "netease"
    assert target.id == "186016"


def test_detect_plain_numeric_id(registry: Registry) -> None:
    target = registry.detect("5257138")
    assert target.id == "5257138"


def test_detect_rejects_garbage(registry: Registry) -> None:
    with pytest.raises(ValueError):
        registry.detect("这不是链接")


# ---------------------------------------------------------------- 命名


@pytest.mark.parametrize(
    "raw,expected",
    [
        ('a/b\\c:d*e?f"g<h>i|j', "a_b_c_d_e_f_g_h_i_j"),
        ("  trailing dots...  ", "trailing dots"),
        ("", "unknown"),
        ("CON", "_CON"),
        ("正常歌名", "正常歌名"),
    ],
)
def test_sanitize(raw: str, expected: str) -> None:
    assert sanitize(raw) == expected


def test_build_stem_default_template(monkeypatch: pytest.MonkeyPatch) -> None:
    from mdl.config import settings

    monkeypatch.setattr(settings, "filename_template", "{artist} - {title}")
    track = Track(provider="netease", id="1", title="晴天", artists=["周杰伦"])
    assert build_stem(track) == "周杰伦 - 晴天"


def test_build_stem_with_index(monkeypatch: pytest.MonkeyPatch) -> None:
    from mdl.config import settings

    monkeypatch.setattr(settings, "filename_template", "{index:02d}. {title}")
    track = Track(provider="netease", id="1", title="晴天", artists=["周杰伦"])
    assert build_stem(track, 3) == "03. 晴天"


def test_build_stem_unknown_artist(monkeypatch: pytest.MonkeyPatch) -> None:
    from mdl.config import settings

    monkeypatch.setattr(settings, "filename_template", "{artist} - {title}")
    track = Track(provider="netease", id="1", title="纯音乐")
    assert build_stem(track) == "未知歌手 - 纯音乐"


def test_unique_path(tmp_path: Path) -> None:
    first = unique_path(tmp_path, "song", "mp3")
    first.write_bytes(b"x")
    second = unique_path(tmp_path, "song", "mp3")
    assert second.name == "song (2).mp3"


# ---------------------------------------------------------------- 模型


def test_track_serialization() -> None:
    track = Track(
        provider="qq",
        id="0039MnYb0qxYhV",
        title="晴天",
        artists=["周杰伦"],
        album="叶惠美",
        duration_ms=269000,
    )
    data = track.to_dict()
    assert data["artist_text"] == "周杰伦"
    assert data["duration_text"] == "04:29"
    assert data["provider"] == "qq"


def test_duration_text_zero() -> None:
    assert Track(provider="qq", id="1", title="x").duration_text == "--:--"


def test_stream_info_roundtrip() -> None:
    info = StreamInfo(url="https://x/y.mp3", ext="mp3", bitrate=320000, size=100, quality="320")
    assert info.to_dict()["quality"] == "320"


# ---------------------------------------------------------------- 音质


@pytest.mark.parametrize(
    "available,wanted,expected",
    [
        ({"128": True, "320": True}, "320", "320"),
        ({"128": True}, "320", "128"),
        ({"128": True, "192": True, "320": True}, "192", "192"),
        ({"flac": True, "320": True}, "320", "320"),
        ({"flac": True}, "320", "flac"),
        ({}, "320", ""),
    ],
)
def test_pick_quality(available: dict[str, bool], wanted: str, expected: str) -> None:
    assert pick_quality(available, wanted) == expected


# ---------------------------------------------------------------- Pages 构建


def test_build_pages_outputs_relative_assets(tmp_path: Path, monkeypatch) -> None:
    """docs/ 产物必须用相对路径，否则 Pages 子路径下 404。"""
    import importlib.util

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("build_pages", root / "scripts" / "build_pages.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)

    monkeypatch.setattr(mod, "OUT", tmp_path / "docs")
    out = mod.build(api="")

    html = (out / "index.html").read_text(encoding="utf-8")
    assert 'href="./app.css"' in html
    assert 'src="./app.js"' in html
    assert "/static/" not in html
    assert (out / "app.js").is_file()
    assert (out / "app.css").is_file()
    assert (out / "404.html").is_file()
    assert (out / ".nojekyll").is_file()


def test_build_pages_injects_api_base(tmp_path: Path, monkeypatch) -> None:
    import importlib.util

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("build_pages", root / "scripts" / "build_pages.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)

    monkeypatch.setattr(mod, "OUT", tmp_path / "docs")
    out = mod.build(api="https://mdl.example.workers.dev")

    html = (out / "index.html").read_text(encoding="utf-8")
    assert "window.MDL_API_BASE" in html
    assert "https://mdl.example.workers.dev" in html


def test_docs_matches_source() -> None:
    """仓库里的 docs/ 不能跟源码漂移——Pages 发的就是它。"""
    root = Path(__file__).resolve().parents[1]
    src = root / "src" / "mdl" / "web"
    docs = root / "docs"
    for name in ("app.css", "app.js"):
        assert (src / name).read_bytes() == (docs / name).read_bytes(), (
            f"docs/{name} 与源码不一致，跑 python scripts/build_pages.py 重新生成"
        )


# ---------------------------------------------------------------- 直连模式


def test_direct_js_ships_with_web() -> None:
    """direct.js 必须和页面一起发出去，否则 Pages 上没有直连能力。"""
    root = Path(__file__).resolve().parents[1]
    direct = root / "src" / "mdl" / "web" / "direct.js"
    assert direct.is_file()
    text = direct.read_text(encoding="utf-8")
    assert "MDLDirect" in text
    assert "JSONP" in text or "jsonp" in text


def test_index_loads_direct_before_app() -> None:
    """direct.js 必须在 app.js 之前加载，app 初始化时要读 window.MDLDirect。"""
    root = Path(__file__).resolve().parents[1]
    html = (root / "src" / "mdl" / "web" / "index.html").read_text(encoding="utf-8")
    i_direct = html.find('src="./direct.js"')
    i_app = html.find('src="./app.js"')
    assert i_direct > 0 and i_app > 0
    assert i_direct < i_app


def test_direct_capabilities_contract() -> None:
    """能力表要和实测一致：酷狗可下、网易云不可搜。"""
    import re

    root = Path(__file__).resolve().parents[1]
    text = (root / "src" / "mdl" / "web" / "direct.js").read_text(encoding="utf-8")
    # 粗粒度校验，避免把 JS 逻辑在 Python 里重写一遍
    assert re.search(r"netease:\s*\{\s*search:\s*false", text)
    assert re.search(r"kugou:\s*\{\s*search:\s*true,\s*download:\s*true", text)
    assert re.search(r"qq:\s*\{\s*search:\s*true,\s*download:\s*false", text)


# ---------------------------------------------------------------- 打包


def test_entry_defaults_to_serve() -> None:
    """双击 exe（无参数）应该起服务并开浏览器，而不是报错退出。"""
    import mdl.entry as entry

    captured = {}

    def fake_main(argv):
        captured["argv"] = argv
        return 0

    orig = entry.cli_main
    entry.cli_main = fake_main
    try:
        entry.main([])
    finally:
        entry.cli_main = orig
    assert captured["argv"] == ["serve", "--open"]


def test_entry_passes_args_through() -> None:
    import mdl.entry as entry

    captured = {}
    orig = entry.cli_main
    entry.cli_main = lambda argv: captured.setdefault("argv", argv) and 0
    try:
        entry.main(["search", "晴天"])
    finally:
        entry.cli_main = orig
    assert captured["argv"] == ["search", "晴天"]


def test_spec_declares_datas_and_hidden_imports() -> None:
    """spec 必须带上前端资源，否则 exe 起来只有 404。"""
    root = Path(__file__).resolve().parents[1]
    spec = (root / "mdl.spec").read_text(encoding="utf-8")
    assert "mdl/web" in spec
    assert "uvicorn.protocols.http.h11_impl" in spec
    assert "COLLECT" in spec  # 目录模式
