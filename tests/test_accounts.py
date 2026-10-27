"""账号层测试：凭据存储的落盘/脱敏，以及 /api/accounts* 的契约。

全部用桩 provider + 临时凭据文件，不出网。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mdl.accounts import AccountStore  # noqa: E402
from mdl.models import AccountInfo, Playlist, Track  # noqa: E402
from mdl.providers.base import AccountError  # noqa: E402
from mdl.server import app, state  # noqa: E402


# ------------------------------------------------------------------ 凭据存储


def test_store_roundtrip(tmp_path: Path) -> None:
    store = AccountStore(tmp_path / "accounts.json")
    assert store.get("netease") == ""
    store.set("netease", "MUSIC_U=abc")
    assert store.get("netease") == "MUSIC_U=abc"
    assert store.has("netease")

    # 重新加载要能读回来（服务重启后登录态不能丢）
    again = AccountStore(tmp_path / "accounts.json")
    assert again.get("netease") == "MUSIC_U=abc"


def test_store_empty_value_clears(tmp_path: Path) -> None:
    store = AccountStore(tmp_path / "accounts.json")
    store.set("qq", "uin=1")
    store.set("qq", "   ")
    assert not store.has("qq")


def test_store_ignores_corrupt_file(tmp_path: Path) -> None:
    path = tmp_path / "accounts.json"
    path.write_text("{ this is not json", encoding="utf-8")
    store = AccountStore(path)  # 不能抛，只是当空处理
    assert store.all() == {}


def test_store_describe_hides_secret(tmp_path: Path) -> None:
    store = AccountStore(tmp_path / "accounts.json")
    store.set("netease", "MUSIC_U=supersecret")
    described = store.describe()
    assert described["netease"]["has_cookie"] is True
    assert "supersecret" not in json.dumps(described)


def test_store_writes_atomically_no_tmp_left(tmp_path: Path) -> None:
    store = AccountStore(tmp_path / "accounts.json")
    store.set("kuwo", "kw_token=x")
    assert [p.name for p in tmp_path.iterdir()] == ["accounts.json"]


# ------------------------------------------------------------------ 桩环境


class StubAccountProvider:
    """一个能力齐全的平台：能验身份、能扫码、有歌单。"""

    name = "netease"
    label = "网易云"
    referer = "https://music.163.com/"
    supports_lyric = True
    login_modes = ("qr", "cookie")
    playlists_supported = True
    account_note = ""

    def __init__(self) -> None:
        self.saved_cookies: list[str] = []

    async def search(self, keyword: str, limit: int = 20) -> list[Track]:
        return []

    async def track_from_id(self, track_id: str) -> Track:
        raise ValueError(track_id)

    async def stream(self, track: Track, quality: str = "320"):
        raise ValueError("stub")

    async def account_info(self, cookie: str) -> AccountInfo:
        if "MUSIC_U" not in cookie:
            raise AccountError(self.name, "Cookie 无效：缺少 MUSIC_U")
        return AccountInfo(
            provider=self.name,
            logged_in=True,
            nickname="听歌的人",
            avatar="https://p1.music.126.net/avatar.jpg",
            uid="42",
            vip=True,
            level="8",
        )

    async def user_playlists(self, cookie: str) -> list[Playlist]:
        return [
            Playlist(
                provider=self.name,
                id="501",
                name="我喜欢的音乐",
                track_count=2,
                cover_url="https://p1.music.126.net/cover.jpg",
                creator="听歌的人",
            )
        ]

    async def playlist_tracks(self, playlist_id: str, cookie: str = ""):
        return "我喜欢的音乐", [
            Track(provider=self.name, id="1", title="屋顶", artists=["周杰伦"]),
            Track(provider=self.name, id="2", title="晴天", artists=["周杰伦"]),
        ]

    async def qr_start(self) -> dict[str, Any]:
        return {
            "key": "qr-key-1",
            "qr_image": "data:image/png;base64,AAAA",
            "qr_text": "https://music.163.com/login?codekey=qr-key-1",
            "expires_in": 300,
        }

    async def qr_poll(self, key: str) -> dict[str, Any]:
        if key != "qr-key-1":
            return {"status": "expired", "message": "二维码已过期"}
        return {
            "status": "ok",
            "message": "登录成功",
            "cookie": "MUSIC_U=from-qr",
            "account": {"provider": self.name, "logged_in": True, "nickname": "扫码进来的号"},
        }


class StubCookieOnlyProvider(StubAccountProvider):
    """只支持粘贴 Cookie、且没有歌单能力的平台（酷狗 / 酷我的形态）。"""

    name = "kugou"
    label = "酷狗"
    login_modes = ("cookie",)
    playlists_supported = False
    account_note = "酷狗账号歌单走 App 端签名接口，本工具只读公开歌单"

    async def account_info(self, cookie: str) -> AccountInfo | None:
        return None  # 平台验不出身份

    async def user_playlists(self, cookie: str):
        raise AccountError(self.name, "不支持")


class StubRegistry:
    def __init__(self) -> None:
        self.providers: dict[str, Any] = {
            "netease": StubAccountProvider(),
            "kugou": StubCookieOnlyProvider(),
        }

    def get(self, name: str) -> Any:
        if name not in self.providers:
            raise ValueError(f"不支持的平台: {name}")
        return self.providers[name]

    def names(self) -> list[str]:
        return list(self.providers)


class StubHttp:
    def __init__(self) -> None:
        self.cookies: dict[str, str] = {}

    def set_cookie(self, provider: str, cookie: str) -> None:
        if cookie:
            self.cookies[provider] = cookie
        else:
            self.cookies.pop(provider, None)


@pytest.fixture()
def acct(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    registry = StubRegistry()
    store = AccountStore(tmp_path / "accounts.json")
    fake_http = StubHttp()
    monkeypatch.setattr(state, "registry", registry, raising=False)
    monkeypatch.setattr(state, "accounts", store, raising=False)
    monkeypatch.setattr(state, "http", fake_http, raising=False)
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test"), store, fake_http


# ------------------------------------------------------------------ 路由契约


async def test_accounts_lists_every_provider(acct) -> None:
    client, _, _ = acct
    async with client as c:
        r = await c.get("/api/accounts")
    body = r.json()
    assert r.status_code == 200 and body["ok"] is True
    ids = [a["provider"] for a in body["accounts"]]
    assert ids == ["netease", "kugou"]
    netease = body["accounts"][0]
    assert netease["has_cookie"] is False
    assert netease["logged_in"] is False
    assert netease["login_mode"] == "qr"
    kugou = body["accounts"][1]
    assert kugou["login_mode"] == "cookie"
    assert kugou["playlists_supported"] is False
    assert kugou["note"].startswith("酷狗账号歌单")


async def test_save_cookie_validates_and_persists(acct) -> None:
    client, store, fake_http = acct
    async with client as c:
        r = await c.post("/api/accounts/netease", json={"cookie": "MUSIC_U=good"})
    body = r.json()
    assert r.status_code == 200
    assert body["account"]["logged_in"] is True
    assert body["account"]["nickname"] == "听歌的人"
    assert body["account"]["vip"] is True
    assert store.get("netease") == "MUSIC_U=good"
    assert fake_http.cookies["netease"] == "MUSIC_U=good"


async def test_save_cookie_invalid_rejected(acct) -> None:
    client, store, _ = acct
    async with client as c:
        r = await c.post("/api/accounts/netease", json={"cookie": "garbage=1"})
    assert r.status_code == 400
    assert "MUSIC_U" in r.json()["detail"]
    assert not store.has("netease")


async def test_save_cookie_empty_rejected(acct) -> None:
    client, _, _ = acct
    async with client as c:
        r = await c.post("/api/accounts/netease", json={"cookie": "  "})
    assert r.status_code == 400


async def test_save_cookie_unverifiable_still_stored(acct) -> None:
    """平台验不出身份（酷狗）时照样存，但如实回报 logged_in=false + has_cookie。"""
    client, store, _ = acct
    async with client as c:
        r = await c.post("/api/accounts/kugou", json={"cookie": "KuGoo=1; kg_mid=2"})
    body = r.json()
    assert r.status_code == 200
    assert body["account"]["has_cookie"] is True
    assert body["account"]["logged_in"] is False
    assert store.get("kugou") == "KuGoo=1; kg_mid=2"


async def test_unknown_provider_400(acct) -> None:
    client, _, _ = acct
    async with client as c:
        r = await c.get("/api/accounts/spotify/playlists")
    assert r.status_code == 400


async def test_delete_clears_cookie(acct) -> None:
    client, store, fake_http = acct
    store.set("netease", "MUSIC_U=good")
    fake_http.cookies["netease"] = "MUSIC_U=good"
    async with client as c:
        r = await c.delete("/api/accounts/netease")
    assert r.status_code == 200
    assert not store.has("netease")
    # 用户凭据必须从共享 client 上撤掉；平台要求的匿名 Cookie 会回落到默认值
    assert "MUSIC_U" not in fake_http.cookies.get("netease", "")


async def test_qr_start_returns_image_and_key(acct) -> None:
    client, _, _ = acct
    async with client as c:
        r = await c.post("/api/accounts/netease/qr")
    body = r.json()
    assert r.status_code == 200
    assert body["key"] == "qr-key-1"
    assert body["qr_image"].startswith("data:image/png;base64,")
    assert body["expires_in"] == 300


async def test_qr_start_rejects_cookie_only_provider(acct) -> None:
    client, _, _ = acct
    async with client as c:
        r = await c.post("/api/accounts/kugou/qr")
    assert r.status_code == 400
    assert "粘贴" in r.json()["detail"]


async def test_qr_poll_ok_stores_cookie(acct) -> None:
    client, store, fake_http = acct
    async with client as c:
        r = await c.get("/api/accounts/netease/qr/qr-key-1")
    body = r.json()
    assert body["status"] == "ok"
    assert body["account"]["nickname"] == "扫码进来的号"
    assert store.get("netease") == "MUSIC_U=from-qr"
    assert fake_http.cookies["netease"] == "MUSIC_U=from-qr"


async def test_qr_poll_expired_does_not_store(acct) -> None:
    client, store, _ = acct
    async with client as c:
        r = await c.get("/api/accounts/netease/qr/unknown-key")
    assert r.json()["status"] == "expired"
    assert not store.has("netease")


async def test_playlists_maps_cover_field(acct) -> None:
    client, store, _ = acct
    store.set("netease", "MUSIC_U=good")
    async with client as c:
        r = await c.get("/api/accounts/netease/playlists")
    body = r.json()
    assert body["ok"] is True
    assert body["playlists"][0]["cover"] == "https://p1.music.126.net/cover.jpg"
    assert body["playlists"][0]["track_count"] == 2


async def test_playlists_unsupported_returns_note(acct) -> None:
    client, store, _ = acct
    store.set("kugou", "KuGoo=1")
    async with client as c:
        r = await c.get("/api/accounts/kugou/playlists")
    body = r.json()
    assert body["ok"] is True and body["playlists"] == []
    assert "签名接口" in body["note"]


async def test_playlist_tracks_returns_track_dicts(acct) -> None:
    client, store, _ = acct
    store.set("netease", "MUSIC_U=good")
    async with client as c:
        r = await c.get("/api/playlists/netease/501")
    body = r.json()
    assert body["title"] == "我喜欢的音乐"
    assert body["total"] == 2
    assert body["tracks"][0]["title"] == "屋顶"
    assert body["tracks"][0]["artists"] == ["周杰伦"]


async def test_cookie_never_echoed(acct) -> None:
    """凭据只进不出：任何账号响应里都不能出现 Cookie 原文。"""
    client, store, _ = acct
    store.set("netease", "MUSIC_U=supersecret")
    async with client as c:
        for path in ("/api/accounts", "/api/accounts/netease/playlists"):
            r = await c.get(path)
            assert "supersecret" not in r.text


async def test_unwritable_account_file_returns_400(tmp_path: Path, monkeypatch) -> None:
    """凭据目录不可写时给可读的 400，不是 500。"""
    blocked = tmp_path / "blocked"
    blocked.write_text("我是个文件，不是目录", encoding="utf-8")
    store = AccountStore(blocked / "accounts.json")
    monkeypatch.setattr(state, "registry", StubRegistry(), raising=False)
    monkeypatch.setattr(state, "accounts", store, raising=False)
    monkeypatch.setattr(state, "http", StubHttp(), raising=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/accounts/netease", json={"cookie": "MUSIC_U=good"})
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "accounts.json" in detail
    assert "MDL_ACCOUNT_FILE" in detail


def test_default_account_file_is_project_cookie_json() -> None:
    """默认落在工作目录的 cookies.json（已 gitignore），不是用户目录。"""
    from mdl.config import Settings, settings

    assert settings.account_file.name == "cookies.json"
    assert not settings.account_file.is_absolute()
    assert Settings().account_file.name == "cookies.json"