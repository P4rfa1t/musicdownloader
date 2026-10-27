"""QQ 音乐 provider。

搜索 / 详情 / 歌词走 musicu.fcg 公开接口，无需登录。
下载直链（vkey）当前对匿名请求返回空 purl，需要登录 Cookie：
把浏览器里的 Cookie 写进 MDL_QQ_COOKIE 即可解锁。
"""

from __future__ import annotations

import base64
import re
from typing import Any

from ..models import ResolveResult, StreamInfo, Track
from .base import Provider, StreamUnavailable, strip_tags

MUSICU = "https://u.y.qq.com/cgi-bin/musicu.fcg"
SMARTBOX = "https://c.y.qq.com/splcloud/fcgi-bin/smartbox_new.fcg"
REFERER = "https://y.qq.com/"

# QQ 返回的 file 字段里各档位的 size 键
SIZE_KEYS = {
    "128": "size_128mp3",
    "192": "size_192aac",
    "320": "size_320mp3",
    "flac": "size_flac",
}
PREFIX = {"128": "M500", "192": "C400", "320": "M800", "flac": "F000"}


class QQProvider(Provider):
    name = "qq"
    referer = REFERER

    # ---------- 底层调用 ----------

    async def _call(self, req: dict[str, Any], ct: str = "24", cv: str = "0") -> dict[str, Any]:
        payload = {"comm": {"ct": ct, "cv": cv, "uin": "0"}, **req}
        data = await self.http.post_json(
            MUSICU,
            payload,
            headers=self.headers(
                {
                    "Content-Type": "application/json;charset=UTF-8",
                    "Origin": "https://y.qq.com",
                }
            ),
        )
        return data or {}

    # ---------- 搜索 ----------

    async def search(self, keyword: str, limit: int = 20) -> list[Track]:
        data = await self._call(
            {
                "req": {
                    "module": "music.search.SearchCgiService",
                    "method": "DoSearchForQQMusicDesktop",
                    "param": {
                        "query": keyword,
                        "num_per_page": min(limit, 50),
                        "page_num": 1,
                        "search_type": 0,
                        "grp": 1,
                    },
                }
            },
            ct="19",
            cv="1859",
        )
        songs = (((data.get("req") or {}).get("data") or {}).get("body") or {}).get("song") or {}
        items = songs.get("list") or []
        if not items:
            # 桌面搜索偶发空结果，退回联想词接口
            return await self._search_smartbox(keyword, limit)
        return [self._track_from_item(s) for s in items]

    async def _search_smartbox(self, keyword: str, limit: int) -> list[Track]:
        data = await self.http.get_json(
            SMARTBOX,
            headers=self.headers(),
            params={
                "key": keyword,
                "format": "json",
                "g_tk": 5381,
                "loginUin": 0,
                "hostUin": 0,
                "inCharset": "utf8",
                "outCharset": "utf-8",
                "platform": "yqq.json",
                "needNewCode": 0,
            },
        )
        items = ((((data or {}).get("data") or {}).get("song") or {}).get("itemlist")) or []
        tracks: list[Track] = []
        for it in items[:limit]:
            try:
                tracks.append(await self.track_from_id(it["mid"]))
            except Exception:  # 联想词里的脏 id 直接跳过
                continue
        return tracks

    def _track_from_item(self, s: dict[str, Any]) -> Track:
        album = s.get("album") or {}
        singers = s.get("singer") or []
        return Track(
            provider=self.name,
            id=s.get("mid") or str(s.get("id") or ""),
            title=strip_tags(s.get("name") or s.get("title") or ""),
            artists=[strip_tags(x.get("name", "")) for x in singers],
            album=strip_tags(album.get("name") or album.get("title") or ""),
            duration_ms=int(s.get("interval") or 0) * 1000,
            cover_url=_album_cover(album.get("mid") or ""),
            source_url=f"https://y.qq.com/n/ryqq/songDetail/{s.get('mid')}",
            extra={"album_mid": album.get("mid"), "song_id": s.get("id")},
        )

    # ---------- 详情 ----------

    async def track_from_id(self, track_id: str) -> Track:
        data = await self._call(
            {
                "req_1": {
                    "module": "music.pf_song_detail_svr",
                    "method": "get_song_detail",
                    "param": {"song_mid": track_id},
                }
            }
        )
        info = ((data.get("req_1") or {}).get("data") or {}).get("track_info") or {}
        if not info:
            raise ValueError(f"QQ 音乐曲目不存在: {track_id}")
        album = info.get("album") or {}
        return Track(
            provider=self.name,
            id=info.get("mid") or track_id,
            title=strip_tags(info.get("name") or info.get("title") or ""),
            artists=[strip_tags(x.get("name", "")) for x in (info.get("singer") or [])],
            album=strip_tags(album.get("name") or album.get("title") or ""),
            duration_ms=int(info.get("interval") or 0) * 1000,
            cover_url=_album_cover(album.get("mid") or ""),
            source_url=f"https://y.qq.com/n/ryqq/songDetail/{info.get('mid') or track_id}",
            extra={
                "album_mid": album.get("mid"),
                "song_id": info.get("id"),
                "file": info.get("file") or {},
            },
        )

    # ---------- 直链 ----------

    async def stream(self, track: Track, quality: str = "320") -> StreamInfo:
        file_info = (track.extra or {}).get("file") or {}
        media_mid = file_info.get("media_mid") or track.id
        order = ["flac", "320", "192", "128"]
        if quality in order:
            order = order[order.index(quality) :]
        songtypes = {"flac": 2, "320": 0, "192": 0, "128": 0}

        for q in order:
            prefix = PREFIX[q]
            filename = f"{prefix}{media_mid}.{'flac' if q == 'flac' else 'm4a'}"
            data = await self._call(
                {
                    "req_0": {
                        "module": "music.vkey.GetVkey",
                        "method": "UrlGetVkey",
                        "param": {
                            "guid": "10000",
                            "songmid": [track.id],
                            "songtype": [songtypes[q]],
                            "uin": "0",
                            "loginflag": 1,
                            "platform": "20",
                            "filename": [filename],
                        },
                    }
                }
            )
            payload = data.get("req_0") or {}
            infos = ((payload.get("data") or {}).get("midurlinfo")) or []
            if not infos:
                continue
            purl = infos[0].get("purl") or ""
            if not purl:
                continue
            sip = ((payload.get("data") or {}).get("sip") or [""])[0]
            if purl.startswith("/"):
                url = sip.rstrip("/") + purl
            elif purl.startswith("http"):
                url = purl
            else:
                url = "https://dl.stream.qqmusic.qq.com/" + purl.lstrip("/")
            size = int(file_info.get(SIZE_KEYS.get(q, "")) or 0)
            return StreamInfo(
                url=url,
                ext="flac" if q == "flac" else "m4a",
                bitrate={"128": 128000, "192": 192000, "320": 320000, "flac": 999000}[q],
                size=size,
                quality=q,
                headers=self.headers({"Referer": REFERER}),
            )

        has_cookie = bool(self.http.headers_for(self.name).get("Cookie"))
        reason = "匿名请求拿不到 vkey，请在 MDL_QQ_COOKIE 里填入登录 Cookie" if not has_cookie else "该曲目需要绿钻/付费"
        raise StreamUnavailable(self.name, track, reason)

    # ---------- 歌词 ----------

    async def lyric(self, track: Track) -> str:
        data = await self._call(
            {
                "req_2": {
                    "module": "music.musichallSong.PlayLyricInfo",
                    "method": "GetPlayLyricInfo",
                    "param": {"songMID": track.id, "songID": 0},
                }
            }
        )
        payload = (data.get("req_2") or {}).get("data") or {}
        raw = payload.get("lyric") or ""
        if not raw:
            return ""
        try:
            return base64.b64decode(raw).decode("utf-8", "replace")
        except Exception:
            return ""

    # ---------- 链接解析 ----------

    _PATTERNS = [
        re.compile(r"y\.qq\.com/n/ryqq/songDetail/([0-9A-Za-z]+)"),
        re.compile(r"y\.qq\.com/songDetail/([0-9A-Za-z]+)"),
        re.compile(r"[?&]songmid=([0-9A-Za-z]+)"),
        re.compile(r"[?&]songMid=([0-9A-Za-z]+)"),
        re.compile(r"^([0-9A-Za-z]{14})$"),
    ]

    def parse_id(self, url: str) -> str | None:
        for pat in self._PATTERNS:
            m = pat.search(url)
            if m:
                return m.group(1)
        return None

    async def resolve_url(self, url: str) -> ResolveResult:
        m = re.search(r"[?&]songid=(\d+)", url)
        if m and not self.parse_id(url):
            # 只有数字 songid 时，用搜索兜底换 mid
            tracks = await self.search(str(m.group(1)), 1)
            if tracks:
                return ResolveResult(
                    title=tracks[0].title,
                    tracks=tracks[:1],
                    provider=self.name,
                    cover_url=tracks[0].cover_url,
                    source_url=url,
                )
        return await super().resolve_url(url)


def _album_cover(album_mid: str, size: int = 300) -> str:
    if not album_mid:
        return ""
    return f"https://y.gtimg.cn/music/photo_new/T002R{size}x{size}M000{album_mid}.jpg"
