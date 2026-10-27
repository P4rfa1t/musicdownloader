"""Provider 基类：统一 search / resolve_url / stream / lyric 接口。"""

from __future__ import annotations

import abc
import re
from typing import Any

from ..http import Http
from ..models import AccountInfo, Playlist, ResolveResult, StreamInfo, Track

_HTML_TAG = re.compile(r"</?[a-zA-Z][^>]*>")


def strip_tags(text: str) -> str:
    """去掉接口返回里的 <em> 高亮标签并还原实体。"""
    if not text:
        return ""
    import html

    return html.unescape(_HTML_TAG.sub("", text)).strip()


class Provider(abc.ABC):
    """一个音乐平台。

    子类必须实现 search / track_from_id / stream；resolve_url 默认走 track_from_id。
    """

    name: str = ""
    # 中文显示名，接口与界面都用它，别在前端再维护一份映射
    label: str = ""
    referer: str = ""
    # 该平台是否提供歌词接口
    supports_lyric: bool = True

    # ---------- 账号能力声明 ----------
    # 支持的登录方式："cookie" 粘贴凭据，"qr" 扫码。顺序即 UI 展示优先顺序。
    login_modes: tuple[str, ...] = ("cookie",)
    # 是否能读该账号的歌单
    playlists_supported: bool = False
    # 能力受限时给用户看的一句话（比如「酷我需要站点 CSRF token」）
    account_note: str = ""

    def __init__(self, http: Http) -> None:
        self.http = http

    # ---------- 必须实现 ----------

    @abc.abstractmethod
    async def search(self, keyword: str, limit: int = 20) -> list[Track]:
        """按关键词搜索单曲。"""

    @abc.abstractmethod
    async def track_from_id(self, track_id: str) -> Track:
        """按平台 id 取完整曲目信息（含封面、时长）。"""

    @abc.abstractmethod
    async def stream(self, track: Track, quality: str = "320") -> StreamInfo:
        """取直链。拿不到时抛 StreamUnavailable。"""

    # ---------- 可选实现 ----------

    async def lyric(self, track: Track) -> str:
        """取 LRC 文本，取不到返回空串。"""
        return ""

    async def resolve_url(self, url: str) -> ResolveResult:
        """把分享链接解析成曲目集合。默认按单曲处理。"""
        track_id = self.parse_id(url)
        if not track_id:
            raise ValueError(f"无法从链接识别 {self.name} 曲目: {url}")
        track = await self.track_from_id(track_id)
        track.source_url = url
        return ResolveResult(
            title=track.title,
            tracks=[track],
            provider=self.name,
            kind="song",
            cover_url=track.cover_url,
            source_url=url,
        )

    def parse_id(self, url: str) -> str | None:
        """从 URL 中提取 id，子类按自身规则覆盖。"""
        return None

    # ---------- 账号（可选实现） ----------

    async def account_info(self, cookie: str) -> AccountInfo | None:
        """校验 cookie 属于谁。拿不到就返回 None，UI 会显示「已保存，未验证」。"""
        return None

    async def user_playlists(self, cookie: str) -> list[Playlist]:
        """当前账号的歌单。"""
        raise AccountError(self.name, "该平台暂不支持读取账号歌单")

    async def playlist_tracks(
        self, playlist_id: str, cookie: str = ""
    ) -> tuple[str, list[Track]]:
        """取歌单内的曲目，返回 (歌单名, 曲目列表)。"""
        raise AccountError(self.name, "该平台暂不支持读取歌单曲目")

    async def qr_start(self) -> dict[str, Any]:
        """申请扫码登录。返回 {key, qr_image, qr_text, expires_in}。

        只有 login_modes 里含 "qr" 的平台才实现。
        """
        raise NotImplementedError(f"{self.name} 不支持扫码登录，请粘贴 Cookie")

    async def qr_poll(self, key: str) -> dict[str, Any]:
        """轮询扫码状态。返回 {status, message, cookie?, account?}。"""
        raise NotImplementedError(f"{self.name} 不支持扫码登录")

    # ---------- 工具 ----------

    def headers_with(
        self, cookie: str = "", extra: dict[str, str] | None = None
    ) -> dict[str, str]:
        """显式指定 cookie（优先于 Http 里登记的），用于账号相关请求。"""
        h = self.http.headers_for(self.name, self.referer)
        if cookie:
            h["Cookie"] = cookie
        if extra:
            h.update(extra)
        return h

    def headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        h = self.http.headers_for(self.name, self.referer)
        if extra:
            h.update(extra)
        return h

    def __repr__(self) -> str:
        return f"<Provider {self.name}>"


class AccountError(RuntimeError):
    """账号相关操作失败（cookie 无效、平台不支持、被风控）。"""

    def __init__(self, provider: str, reason: str) -> None:
        self.provider = provider
        self.reason = reason
        super().__init__(f"[{provider}] {reason}")


class StreamUnavailable(RuntimeError):
    """该曲目在当前账号/地区下没有可下载直链（版权、付费、下架）。"""

    def __init__(self, provider: str, track: Track, reason: str = "") -> None:
        self.provider = provider
        self.track = track
        self.reason = reason
        msg = f"[{provider}] 《{track.title}》无可下载直链"
        if reason:
            msg += f"：{reason}"
        super().__init__(msg)


def pick_quality(available: dict[str, Any], wanted: str) -> str:
    """在平台提供的档位里，选不高于 wanted 的最高档；都没有则退而取最低的更高档。"""
    order = ["128", "192", "320", "flac"]
    if wanted not in order:
        wanted = "320"
    ceiling = order.index(wanted)
    for q in reversed(order[: ceiling + 1]):
        if available.get(q):
            return q
    # 平台只提供更高档位时，取其中最低的一档（有损优先，体积更小）
    for q in order[ceiling + 1 :]:
        if available.get(q):
            return q
    return ""
