"""酷狗音乐 provider。

搜索走 song_search_v2，直链走 m.kugou.com 的 playInfo 接口（免费曲目直接给完整 mp3）。
歌词走 krcs.kugou.com。
"""

from __future__ import annotations

import base64
import re
from typing import Any

from ..models import ResolveResult, StreamInfo, Track
from .base import Provider, StreamUnavailable, strip_tags

SEARCH = "https://songsearch.kugou.com/song_search_v2"
PLAYINFO = "https://m.kugou.com/app/i/getSongInfo.php"
KRC_SEARCH = "https://krcs.kugou.com/search"
KRC_DOWNLOAD = "https://lyrics.kugou.com/download"
REFERER = "https://www.kugou.com/"

# 档位 -> (搜索项里的 hash 字段, 对应码率)
HASH_KEYS = {
    "128": ("FileHash", 128000),
    "320": ("HQFileHash", 320000),
    "flac": ("SQFileHash", 999000),
}


class KugouProvider(Provider):
    name = "kugou"
    referer = REFERER

    # ---------- 搜索 ----------

    async def search(self, keyword: str, limit: int = 20) -> list[Track]:
        data = await self.http.get_json(
            SEARCH,
            headers=self.headers(),
            params={
                "keyword": keyword,
                "page": 1,
                "pagesize": min(limit, 50),
                "platform": "WebFilter",
                "tag": "em",
                "filter": 2,
                "iscorrection": 1,
                "privilege_filter": 0,
            },
        )
        items = ((data or {}).get("data") or {}).get("lists") or []
        return [self._track_from_item(it) for it in items]

    def _track_from_item(self, it: dict[str, Any]) -> Track:
        hashes = {
            "128": it.get("FileHash") or "",
            "320": it.get("HQFileHash") or "",
            "flac": it.get("SQFileHash") or "",
        }
        image = it.get("Image") or it.get("AlbumImage") or ""
        if image:
            image = image.replace("{size}", "480")
        return Track(
            provider=self.name,
            id=(it.get("FileHash") or "").lower(),
            title=strip_tags(it.get("SongName", "")),
            artists=[
                strip_tags(s.get("name", ""))
                for s in (it.get("Singers") or [])
                if s.get("name")
            ]
            or [strip_tags(it.get("SingerName", ""))],
            album=strip_tags(it.get("AlbumName", "")),
            duration_ms=int(it.get("Duration") or 0) * 1000,
            cover_url=image,
            source_url=f"https://www.kugou.com/song/#hash={(it.get('FileHash') or '').lower()}",
            extra={
                "hashes": hashes,
                "album_id": it.get("AlbumID"),
                "audio_id": it.get("Audioid") or it.get("audio_id"),
                "privilege": it.get("Privilege"),
                "pay_type": it.get("PayType"),
            },
        )

    # ---------- 详情 ----------

    async def track_from_id(self, track_id: str) -> Track:
        data = await self.http.get_json(
            PLAYINFO,
            headers=self.headers({"Referer": "https://m.kugou.com/"}),
            params={"cmd": "playInfo", "hash": track_id},
        )
        if not data or not data.get("songName"):
            raise ValueError(f"酷狗曲目不存在: {track_id}")
        extra = data.get("extra") or {}
        hashes = {
            "128": (extra.get("128hash") or data.get("hash") or "").lower(),
            "320": (extra.get("320hash") or "").lower(),
            "flac": (extra.get("sqhash") or "").lower(),
        }
        return Track(
            provider=self.name,
            id=track_id.lower(),
            title=strip_tags(data.get("songName", "")),
            artists=[strip_tags(data.get("author_name") or data.get("singerName") or "")],
            album=strip_tags(data.get("album_name") or ""),
            duration_ms=int(data.get("timeLength") or 0),
            cover_url=(data.get("imgUrl") or "").replace("{size}", "480"),
            source_url=f"https://www.kugou.com/song/#hash={track_id.lower()}",
            extra={
                "hashes": hashes,
                "album_id": data.get("albumid") or data.get("req_albumid"),
                "audio_id": data.get("audio_id"),
                "timelength": data.get("timeLength"),
            },
        )

    # ---------- 直链 ----------

    async def stream(self, track: Track, quality: str = "320") -> StreamInfo:
        hashes: dict[str, str] = (track.extra or {}).get("hashes") or {}
        order = ["flac", "320", "128"]
        if quality in order:
            order = order[order.index(quality) :]
        for q in order:
            h = hashes.get(q) or (track.id if q == "128" else "")
            if not h:
                continue
            data = await self.http.get_json(
                PLAYINFO,
                headers=self.headers({"Referer": "https://m.kugou.com/"}),
                params={"cmd": "playInfo", "hash": h},
            )
            url = (data or {}).get("url") or ""
            if not url:
                continue
            actual = int((data or {}).get("bitRate") or 0)
            # playInfo 有时忽略 hash 的档位，按返回码率校正
            level = "128" if actual <= 128 else ("320" if actual <= 320 else "flac")
            return StreamInfo(
                url=url,
                ext=(data or {}).get("extName") or "mp3",
                bitrate=actual * 1000 if actual else 0,
                size=int((data or {}).get("fileSize") or 0),
                quality=level,
                headers=self.headers({"Referer": "https://m.kugou.com/"}),
            )
        raise StreamUnavailable(self.name, track, "该曲目在酷狗需要付费或已下架")

    # ---------- 歌词 ----------

    async def lyric(self, track: Track) -> str:
        duration = int(track.duration_ms / 1000) if track.duration_ms else 0
        found = await self.http.get_json(
            KRC_SEARCH,
            headers=self.headers(),
            params={
                "ver": 1,
                "man": "yes",
                "client": "mobi",
                "keyword": "",
                "duration": duration,
                "hash": track.id,
            },
        )
        candidates = (found or {}).get("candidates") or []
        if not candidates:
            return ""
        cand = candidates[0]
        data = await self.http.get_json(
            KRC_DOWNLOAD,
            headers=self.headers(),
            params={
                "ver": 1,
                "client": "pc",
                "id": cand.get("id"),
                "accesskey": cand.get("accesskey"),
                "fmt": "lrc",
                "charset": "utf8",
            },
        )
        content = (data or {}).get("content") or ""
        if not content:
            return ""
        try:
            return base64.b64decode(content).decode("utf-8", "replace")
        except Exception:
            return ""

    # ---------- 链接解析 ----------

    _PATTERNS = [
        re.compile(r"kugou\.com/song/#?hash=([0-9A-Fa-f]{32})"),
        re.compile(r"kugou\.com/mixsong/([0-9A-Fa-f]{32})"),
        re.compile(r"[?&]hash=([0-9A-Fa-f]{32})"),
        re.compile(r"^([0-9A-Fa-f]{32})$"),
    ]

    def parse_id(self, url: str) -> str | None:
        for pat in self._PATTERNS:
            m = pat.search(url)
            if m:
                return m.group(1).lower()
        return None

    async def resolve_url(self, url: str) -> ResolveResult:
        # 酷狗分享短链 https://t1.kugou.com/xxxx 需要跟随跳转
        if "kugou.com" in url and not self.parse_id(url) and "song/" not in url:
            try:
                resp = await self.http.request(
                    "GET", url, headers=self.headers({"Referer": REFERER})
                )
                final = str(resp.url)
                tid = self.parse_id(final)
                if tid:
                    url = final
            except Exception:
                pass
        return await super().resolve_url(url)
