"""数据模型：曲目、音质、直链、解析目标。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

ProviderName = Literal["netease", "qq", "kugou", "kuwo"]

# 音质档位，从低到高。各平台按自身能力映射。
QUALITIES: list[str] = ["128", "192", "320", "flac"]


@dataclass
class Track:
    """一首歌的最小可用描述。id 是平台原生 id（网易云数字、QQ mid、酷狗 hash、酷我 rid）。"""

    provider: str
    id: str
    title: str
    artists: list[str] = field(default_factory=list)
    album: str = ""
    duration_ms: int = 0
    cover_url: str = ""
    source_url: str = ""
    # 平台私有字段，例如 QQ 的 album_mid、酷狗的多档 hash
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def artist_text(self) -> str:
        return " / ".join(a for a in self.artists if a) or "未知歌手"

    @property
    def duration_text(self) -> str:
        if self.duration_ms <= 0:
            return "--:--"
        total = round(self.duration_ms / 1000)
        return f"{total // 60:02d}:{total % 60:02d}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "id": self.id,
            "title": self.title,
            "artists": self.artists,
            "artist_text": self.artist_text,
            "album": self.album,
            "duration_ms": self.duration_ms,
            "duration_text": self.duration_text,
            "cover_url": self.cover_url,
            "source_url": self.source_url,
            "extra": self.extra,
        }


@dataclass
class StreamInfo:
    """一条可下载的直链。"""

    url: str
    ext: str = "mp3"
    bitrate: int = 0
    size: int = 0
    quality: str = "320"
    headers: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "ext": self.ext,
            "bitrate": self.bitrate,
            "size": self.size,
            "quality": self.quality,
        }


@dataclass
class Target:
    """从 URL 解析出的目标：单曲 / 歌单 / 专辑 / 榜单。"""

    provider: str
    kind: Literal["song", "playlist", "album"]
    id: str
    raw_url: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "kind": self.kind,
            "id": self.id,
            "raw_url": self.raw_url,
        }


@dataclass
class AccountInfo:
    """一个平台上「当前 Cookie 对应的是谁」。"""

    provider: str
    logged_in: bool = False
    nickname: str = ""
    avatar: str = ""
    uid: str = ""
    vip: bool = False
    # 平台自己喊的等级名（如网易云 0-10），没有则空
    level: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "logged_in": self.logged_in,
            "nickname": self.nickname,
            "avatar": self.avatar,
            "uid": self.uid,
            "vip": self.vip,
            "level": self.level,
        }


@dataclass
class Playlist:
    """账号下的一个歌单（也用于公开歌单）。"""

    provider: str
    id: str
    name: str
    track_count: int = 0
    cover_url: str = ""
    creator: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "track_count": self.track_count,
            "cover": self.cover_url,
            "creator": self.creator,
        }


@dataclass
class ResolveResult:
    """解析结果：可能是单曲，也可能是歌单展开后的多曲。"""

    title: str
    tracks: list[Track]
    provider: str
    kind: str = "song"
    cover_url: str = ""
    source_url: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "provider": self.provider,
            "kind": self.kind,
            "cover_url": self.cover_url,
            "source_url": self.source_url,
            "count": len(self.tracks),
            "tracks": [t.to_dict() for t in self.tracks],
        }
