"""网易云音乐 provider。

接口走公开 web API（/api/*），无需登录即可搜索、取歌词、取 128/320k 直链；
带 Cookie 时能拿到更高码率与更多版权曲目。
"""

from __future__ import annotations

import re
from typing import Any

from ..models import ResolveResult, StreamInfo, Track
from .base import Provider, StreamUnavailable, strip_tags

API = "https://music.163.com"
OUTER = "https://music.163.com/song/media/outer/url?id={id}.mp3"

# br 值 -> 档位名
BR_MAP = {128000: "128", 192000: "192", 320000: "320", 999000: "flac"}


class NeteaseProvider(Provider):
    name = "netease"
    referer = "https://music.163.com/"

    # ---------- 搜索 ----------

    async def search(self, keyword: str, limit: int = 20) -> list[Track]:
        data = await self.http.get_json(
            f"{API}/api/search/get/web",
            headers=self.headers(),
            params={"s": keyword, "type": 1, "limit": limit, "offset": 0},
        )
        songs = ((data or {}).get("result") or {}).get("songs") or []
        tracks = [self._track_from_search(s) for s in songs]
        if tracks:
            # 搜索接口不返回封面，批量补一次详情
            try:
                await self._enrich(tracks)
            except Exception:
                pass
        return tracks

    async def _enrich(self, tracks: list[Track]) -> None:
        """用 v3 批量详情补齐封面、专辑、时长。"""
        import json as _json

        ids = [{"id": int(t.id)} for t in tracks if t.id.isdigit()]
        if not ids:
            return
        data = await self.http.get_json(
            f"{API}/api/v3/song/detail",
            headers=self.headers(),
            params={"c": _json.dumps(ids, separators=(",", ":"))},
        )
        by_id = {str(s.get("id")): s for s in ((data or {}).get("songs") or [])}
        for t in tracks:
            s = by_id.get(t.id)
            if not s:
                continue
            al = s.get("al") or {}
            t.cover_url = t.cover_url or al.get("picUrl") or ""
            t.album = t.album or strip_tags(al.get("name", ""))
            if not t.duration_ms:
                t.duration_ms = int(s.get("dt") or 0)
            if not t.artists:
                t.artists = [strip_tags(a.get("name", "")) for a in (s.get("ar") or [])]
            t.extra.setdefault("album_id", al.get("id"))
            if s.get("fee") is not None:
                t.extra.setdefault("fee", s.get("fee"))

    def _track_from_search(self, s: dict[str, Any]) -> Track:
        album = s.get("album") or {}
        return Track(
            provider=self.name,
            id=str(s.get("id")),
            title=strip_tags(s.get("name", "")),
            artists=[strip_tags(a.get("name", "")) for a in (s.get("artists") or [])],
            album=strip_tags(album.get("name", "")),
            duration_ms=int(s.get("duration") or 0),
            cover_url=album.get("picUrl") or "",
            source_url=f"{API}/song?id={s.get('id')}",
            extra={"fee": s.get("fee"), "album_id": album.get("id")},
        )

    # ---------- 详情 ----------

    async def track_from_id(self, track_id: str) -> Track:
        data = await self.http.get_json(
            f"{API}/api/song/detail/",
            headers=self.headers(),
            params={"ids": f"[{track_id}]"},
        )
        songs = (data or {}).get("songs") or []
        if not songs:
            raise ValueError(f"网易云曲目不存在: {track_id}")
        s = songs[0]
        album = s.get("album") or {}
        return Track(
            provider=self.name,
            id=str(s.get("id")),
            title=strip_tags(s.get("name", "")),
            artists=[strip_tags(a.get("name", "")) for a in (s.get("artists") or [])],
            album=strip_tags(album.get("name", "")),
            duration_ms=int(s.get("duration") or 0),
            cover_url=album.get("picUrl") or "",
            source_url=f"{API}/song?id={s.get('id')}",
            extra={"fee": s.get("fee"), "album_id": album.get("id")},
        )

    # ---------- 直链 ----------

    async def stream(self, track: Track, quality: str = "320") -> StreamInfo:
        # 依次尝试 999000(无损) / 320000 / 192000 / 128000，取第一个可用
        trial_seen = False
        for br in (999000, 320000, 192000, 128000):
            data = await self.http.get_json(
                f"{API}/api/song/enhance/player/url",
                headers=self.headers(),
                params={"ids": f"[{track.id}]", "br": br},
            )
            items = (data or {}).get("data") or []
            if not items:
                continue
            item = items[0]
            url = item.get("url")
            if not url:
                continue
            # 会员曲目会返回 45 秒试听片段，标记为 freeTrialInfo，不能当正片用
            if item.get("freeTrialInfo"):
                trial_seen = True
                continue
            actual_br = int(item.get("br") or br)
            level = BR_MAP.get(actual_br, "320")
            # 用户指定档位更低时，只接受不高于它的结果
            if _rank(level) > _rank(quality):
                continue
            return StreamInfo(
                url=url,
                ext=item.get("type") or "mp3",
                bitrate=actual_br,
                size=int(item.get("size") or 0),
                quality=level,
                headers=self.headers({"Referer": API + "/"}),
            )
        # 兜底：外链接口（128k）。会员曲目这里会跳 404 页面，交由下载层按大小校验拦截
        if not trial_seen and quality in ("128", "192", "320"):
            return StreamInfo(
                url=OUTER.format(id=track.id),
                ext="mp3",
                bitrate=128000,
                quality="128",
                headers=self.headers({"Referer": API + "/"}),
            )
        if trial_seen:
            raise StreamUnavailable(self.name, track, "会员专享，只提供 45 秒试听片段")
        raise StreamUnavailable(self.name, track, "版权受限或需要会员")

    # ---------- 歌词 ----------

    async def lyric(self, track: Track) -> str:
        data = await self.http.get_json(
            f"{API}/api/song/lyric",
            headers=self.headers(),
            params={"id": track.id, "lv": 1, "kv": 1, "tv": -1},
        )
        lrc = ((data or {}).get("lrc") or {}).get("lyric") or ""
        tlrc = ((data or {}).get("tlyric") or {}).get("lyric") or ""
        if lrc and tlrc:
            return lrc.rstrip() + "\n\n" + tlrc.lstrip()
        return lrc

    # ---------- 链接解析 ----------

    _ID_PATTERNS = [
        re.compile(r"music\.163\.com/(?:#/)?song\?id=(\d+)"),
        re.compile(r"music\.163\.com/song/media/outer/url\?id=(\d+)"),
        re.compile(r"[?&]id=(\d+)"),
        re.compile(r"^(\d{3,})$"),
    ]
    _PLAYLIST_PATTERNS = [
        re.compile(r"music\.163\.com/(?:#/)?playlist\?id=(\d+)"),
        re.compile(r"music\.163\.com/discover/toplist\?id=(\d+)"),
    ]
    _ALBUM_PATTERNS = [re.compile(r"music\.163\.com/(?:#/)?album\?id=(\d+)")]

    def parse_id(self, url: str) -> str | None:
        for pat in self._ID_PATTERNS:
            m = pat.search(url)
            if m:
                return m.group(1)
        return None

    async def resolve_url(self, url: str) -> ResolveResult:
        for pat in self._PLAYLIST_PATTERNS:
            m = pat.search(url)
            if m:
                return await self.playlist(m.group(1), url)
        for pat in self._ALBUM_PATTERNS:
            m = pat.search(url)
            if m:
                return await self.album(m.group(1), url)
        return await super().resolve_url(url)

    async def playlist(self, playlist_id: str, url: str = "") -> ResolveResult:
        data = await self.http.get_json(
            f"{API}/api/v6/playlist/detail",
            headers=self.headers(),
            params={"id": playlist_id, "n": 1000},
        )
        pl = (data or {}).get("playlist") or {}
        tracks = [self._track_from_playlist(t) for t in (pl.get("tracks") or [])]
        if not tracks:
            raise ValueError(f"歌单为空或不存在: {playlist_id}")
        return ResolveResult(
            title=pl.get("name") or f"歌单 {playlist_id}",
            tracks=tracks,
            provider=self.name,
            kind="playlist",
            cover_url=pl.get("coverImgUrl") or "",
            source_url=url or f"{API}/playlist?id={playlist_id}",
        )

    def _track_from_playlist(self, t: dict[str, Any]) -> Track:
        al = t.get("al") or t.get("album") or {}
        ar = t.get("ar") or t.get("artists") or []
        return Track(
            provider=self.name,
            id=str(t.get("id")),
            title=strip_tags(t.get("name", "")),
            artists=[strip_tags(a.get("name", "")) for a in ar],
            album=strip_tags(al.get("name", "")),
            duration_ms=int(t.get("dt") or t.get("duration") or 0),
            cover_url=al.get("picUrl") or "",
            source_url=f"{API}/song?id={t.get('id')}",
            extra={"fee": t.get("fee"), "album_id": al.get("id")},
        )

    async def album(self, album_id: str, url: str = "") -> ResolveResult:
        data = await self.http.get_json(f"{API}/api/album/{album_id}", headers=self.headers())
        al = (data or {}).get("album") or {}
        tracks = [self._track_from_album_song(s, al) for s in (al.get("songs") or [])]
        if not tracks:
            raise ValueError(f"专辑为空或不存在: {album_id}")
        return ResolveResult(
            title=al.get("name") or f"专辑 {album_id}",
            tracks=tracks,
            provider=self.name,
            kind="album",
            cover_url=al.get("picUrl") or "",
            source_url=url or f"{API}/album?id={album_id}",
        )

    def _track_from_album_song(self, s: dict[str, Any], al: dict[str, Any]) -> Track:
        return Track(
            provider=self.name,
            id=str(s.get("id")),
            title=strip_tags(s.get("name", "")),
            artists=[strip_tags(a.get("name", "")) for a in (s.get("artists") or [])],
            album=strip_tags(al.get("name", "")),
            duration_ms=int(s.get("duration") or 0),
            cover_url=al.get("picUrl") or "",
            source_url=f"{API}/song?id={s.get('id')}",
            extra={"fee": s.get("fee"), "album_id": al.get("id")},
        )


def _rank(level: str) -> int:
    return {"128": 0, "192": 1, "320": 2, "flac": 3}.get(level, 0)
