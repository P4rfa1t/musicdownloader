"""酷我音乐 provider。

搜索走 search.kuwo.cn 的老接口（返回 JSON，用单引号，需要归一化）。
直链走 nmobi 的 convert_url2 接口。注意：匿名状态下酷我多数曲目只给
试听片段（约 16KB），因此 provider 会校验文件长度，片段直接判为不可下载。
带 Cookie（MDL_KUWO_COOKIE）时才有机会拿到完整文件。
"""

from __future__ import annotations

import re
from typing import Any

from ..models import ResolveResult, StreamInfo, Track
from .base import Provider, StreamUnavailable, strip_tags

SEARCH = "http://search.kuwo.cn/r.s"
PLAY = "http://nmobi.kuwo.cn/mobi.s"
REFERER = "http://www.kuwo.cn/"

# 档位 -> convert_url2 的 br 参数
BR_PARAM = {"128": "128kmp3", "192": "192kmp3", "320": "320kmp3", "flac": "2000kflac"}


class KuwoProvider(Provider):
    name = "kuwo"
    referer = REFERER

    # ---------- 搜索 ----------

    async def search(self, keyword: str, limit: int = 20) -> list[Track]:
        text = await self.http.get_text(
            SEARCH,
            headers=self.headers(),
            params={
                "all": keyword,
                "ft": "music",
                "itemset": "web_2013",
                "client": "kt",
                "pn": 0,
                "rn": min(limit, 50),
                "rformat": "json",
                "encoding": "utf8",
                "ver": "mbox",
                "vipver": 1,
                "show_copyright_off": 1,
                "pcmp4": 1,
                "isbang": 1,
                "plt": "web",
                "sms_ver": 1,
                "module": "search",
            },
        )
        items = _parse_loose_json(text).get("abslist") or []
        return [self._track_from_item(it) for it in items]

    def _track_from_item(self, it: dict[str, Any]) -> Track:
        rid = str(it.get("DC_TARGETID") or it.get("MUSICRID", "").replace("MUSIC_", ""))
        artist = strip_tags(it.get("ARTIST", "")) or strip_tags(it.get("AARTIST", ""))
        formats = (it.get("FORMATS") or "").split("|")
        return Track(
            provider=self.name,
            id=rid,
            title=strip_tags(it.get("NAME") or it.get("SONGNAME") or ""),
            artists=[artist] if artist else [],
            album=strip_tags(it.get("ALBUM", "")),
            duration_ms=int(it.get("DURATION") or 0) * 1000,
            cover_url=_cover(it),
            source_url=f"https://www.kuwo.cn/play_detail/{rid}",
            extra={"formats": formats, "copyright": it.get("COPYRIGHT")},
        )

    # ---------- 详情 ----------

    async def track_from_id(self, track_id: str) -> Track:
        # 酷我没有稳定的单曲详情接口，用 musicid 查询搜索接口的兜底形态
        text = await self.http.get_text(
            SEARCH,
            headers=self.headers(),
            params={
                "musicid": track_id,
                "rformat": "json",
                "encoding": "utf8",
                "ver": "mbox",
                "vipver": 1,
                "show_copyright_off": 1,
                "pcmp4": 1,
                "isbang": 1,
                "plt": "web",
                "sms_ver": 1,
                "module": "music",
                "uid": 0,
                "dev": 1,
            },
        )
        items = _parse_loose_json(text).get("abslist") or []
        for it in items:
            if str(it.get("DC_TARGETID")) == str(track_id):
                return self._track_from_item(it)
        if items:
            return self._track_from_item(items[0])
        # 兜底：只有 id 时也允许下载，标题留空由调用方补齐
        return Track(
            provider=self.name,
            id=str(track_id),
            title=f"kuwo-{track_id}",
            cover_url="",
            source_url=f"https://www.kuwo.cn/play_detail/{track_id}",
            extra={},
        )

    # ---------- 直链 ----------

    async def stream(self, track: Track, quality: str = "320") -> StreamInfo:
        order = ["flac", "320", "128"]
        if quality in order:
            order = order[order.index(quality) :]
        for q in order:
            text = await self.http.get_text(
                PLAY,
                headers=self.headers(),
                params={
                    "f": "web",
                    "type": "convert_url2",
                    "br": BR_PARAM[q],
                    "sig": 0,
                    "format": "mp3",
                    "rid": track.id,
                },
            )
            m = re.search(r"url=(http[^\s\r\n]+)", text or "")
            if not m:
                continue
            url = m.group(1).strip()
            size = await self._content_length(url)
            expected = _expected_size(track.duration_ms, q)
            if size and expected and size < expected * 0.35:
                # 试听片段，换档位也没用，直接判定
                raise StreamUnavailable(
                    self.name,
                    track,
                    f"仅返回试听片段（{size} 字节），需要登录 Cookie 才能取完整文件",
                )
            return StreamInfo(
                url=url,
                ext="mp3",
                bitrate={"128": 128000, "192": 192000, "320": 320000, "flac": 999000}[q],
                size=size,
                quality=q,
                headers=self.headers(),
            )
        raise StreamUnavailable(self.name, track, "酷我未返回可用直链")

    async def _content_length(self, url: str) -> int:
        try:
            resp = await self.http.request("GET", url, headers=self.headers())
            return int(resp.headers.get("Content-Length") or 0)
        except Exception:
            return 0

    # ---------- 歌词 ----------

    async def lyric(self, track: Track) -> str:
        # 酷我歌词是 zlib + 异或加密，解密算法不稳定，这里只取明文接口
        try:
            text = await self.http.get_text(
                PLAY,
                headers=self.headers(),
                params={"f": "web", "type": "lyric", "rid": track.id, "lrcx": 0},
            )
        except Exception:
            return ""
        if "[ti:" in text or "[00:" in text:
            return text
        return ""

    # ---------- 链接解析 ----------

    _PATTERNS = [
        re.compile(r"kuwo\.cn/play_detail/(\d+)"),
        re.compile(r"kuwo\.cn/newh5/singles/songinfoandlrc\?musicId=(\d+)"),
        re.compile(r"[?&]rid=(?:MUSIC_)?(\d+)"),
        re.compile(r"[?&]musicId=(\d+)"),
        re.compile(r"^(\d{4,})$"),
    ]

    def parse_id(self, url: str) -> str | None:
        for pat in self._PATTERNS:
            m = pat.search(url)
            if m:
                return m.group(1)
        return None


def _parse_loose_json(text: str) -> dict[str, Any]:
    """酷我返回的是单引号 JSON，先尝试标准解析，失败再替换引号。"""
    import json

    text = (text or "").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except ValueError:
        pass
    fixed = text.replace("'", '"')
    try:
        return json.loads(fixed)
    except ValueError:
        # 最后尝试补全常见未加引号的 key
        fixed = re.sub(r"([{,])\s*([A-Za-z_][A-Za-z0-9_]*)\s*:", r'\1"\2":', fixed)
        try:
            return json.loads(fixed)
        except ValueError:
            return {}


def _expected_size(duration_ms: int, quality: str) -> int:
    """按码率估算完整文件大小（字节）。"""
    if duration_ms <= 0:
        return 0
    kbps = {"128": 128, "192": 192, "320": 320, "flac": 900}[quality]
    return int(duration_ms / 1000 * kbps * 1000 / 8)


def _cover(item: dict[str, Any]) -> str:
    """酷我搜索项里带 web_albumpic_short（形如 120/s3s94/93/211513640.jpg），
    把尺寸段换成 500 就能拿到大图。"""
    short = (item.get("web_albumpic_short") or "").strip()
    if not short:
        return ""
    big = re.sub(r"^\d+/", "500/", short, count=1)
    return f"https://img1.kuwo.cn/star/albumcover/{big}"
