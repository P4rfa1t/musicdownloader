"""写标签：封面 + 歌词 + 基础元数据，按扩展名分派到 mutagen。"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, TALB, TIT2, TPE1, USLT, ID3, ID3NoHeaderError
from mutagen.mp4 import MP4, MP4Cover
from mutagen.mp3 import MP3

from .models import Track

log = logging.getLogger("mdl.tagger")


def tag_file(path: Path, track: Track, *, cover: bytes = b"", lyric: str = "") -> bool:
    """尽力写入标签，失败只记日志不影响下载结果。"""
    try:
        suffix = path.suffix.lower()
        if suffix == ".mp3":
            _tag_mp3(path, track, cover, lyric)
        elif suffix in (".m4a", ".mp4", ".aac"):
            _tag_mp4(path, track, cover, lyric)
        elif suffix == ".flac":
            _tag_flac(path, track, cover, lyric)
        else:
            return False
        return True
    except Exception as exc:  # 标签失败不能毁掉已下载的文件
        log.warning("写标签失败 %s: %s", path.name, exc)
        return False


def _tag_mp3(path: Path, track: Track, cover: bytes, lyric: str) -> None:
    audio = MP3(str(path))
    try:
        tags = audio.tags if audio.tags is not None else ID3()
    except ID3NoHeaderError:
        tags = ID3()
    tags.setall("TIT2", [TIT2(encoding=3, text=[track.title])])
    tags.setall("TPE1", [TPE1(encoding=3, text=track.artists or ["未知歌手"])])
    if track.album:
        tags.setall("TALB", [TALB(encoding=3, text=[track.album])])
    if lyric:
        tags.setall(
            "USLT", [USLT(encoding=3, lang="chi", desc="", text=lyric)]
        )
    if cover:
        tags.setall(
            "APIC",
            [APIC(encoding=3, mime=_image_mime(cover), type=3, desc="Cover", data=cover)],
        )
    audio.tags = tags
    audio.save(v2_version=3)


def _tag_mp4(path: Path, track: Track, cover: bytes, lyric: str) -> None:
    audio = MP4(str(path))
    audio["\xa9nam"] = [track.title]
    audio["\xa9ART"] = [track.artist_text]
    if track.album:
        audio["\xa9alb"] = [track.album]
    if lyric:
        audio["\xa9lyr"] = [lyric]
    if cover:
        audio["covr"] = [
            MP4Cover(cover, imageformat=MP4Cover.FORMAT_JPEG if _is_jpeg(cover) else MP4Cover.FORMAT_PNG)
        ]
    audio.save()


def _tag_flac(path: Path, track: Track, cover: bytes, lyric: str) -> None:
    audio = FLAC(str(path))
    audio["title"] = track.title
    audio["artist"] = track.artist_text
    if track.album:
        audio["album"] = track.album
    if lyric:
        audio["lyrics"] = lyric
    if cover:
        pic = Picture()
        pic.type = 3
        pic.mime = _image_mime(cover)
        pic.desc = "Cover"
        pic.data = cover
        audio.clear_pictures()
        audio.add_picture(pic)
    audio.save()


def _is_jpeg(data: bytes) -> bool:
    return data[:2] == b"\xff\xd8"


def _image_mime(data: bytes) -> str:
    if _is_jpeg(data):
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    return "image/jpeg"


def probe_duration(path: Path) -> int:
    """读音频实际时长（毫秒），失败返回 0。"""
    try:
        from mutagen import File as MutagenFile

        f: Any = MutagenFile(str(path))
        if f is not None and f.info is not None:
            return int(float(f.info.length) * 1000)
    except Exception:
        pass
    return 0
