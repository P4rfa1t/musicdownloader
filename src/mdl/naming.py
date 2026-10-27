"""文件名与目录规则。"""

from __future__ import annotations

import re
from pathlib import Path

from .config import settings
from .models import Track

# Windows 非法字符 + 控制字符
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_MAX_STEM = 120


def sanitize(name: str) -> str:
    """把任意文本变成安全的 Windows 文件名片段。"""
    text = _ILLEGAL.sub("_", (name or "").strip())
    text = re.sub(r"\s+", " ", text).strip(" .")
    if not text:
        return "unknown"
    if text.upper().split(".")[0] in _RESERVED:
        text = "_" + text
    return text[:_MAX_STEM].rstrip(" .") or "unknown"


def build_stem(track: Track, index: int = 0) -> str:
    """按模板渲染不带扩展名的文件名。"""
    template = settings.filename_template or "{artist} - {title}"
    try:
        stem = template.format(
            artist=track.artist_text,
            title=track.title,
            album=track.album or "未知专辑",
            index=index,
            provider=track.provider,
        )
    except (KeyError, IndexError):
        stem = f"{track.artist_text} - {track.title}"
    return sanitize(stem)


def build_dir() -> Path:
    """按子目录模板算出下载目录。"""
    base = Path(settings.download_dir)
    tpl = (settings.subdir_template or "").strip()
    if not tpl:
        return base
    safe = "/".join(sanitize(part) for part in tpl.split("/") if part.strip())
    return base / safe if safe else base


def unique_path(directory: Path, stem: str, ext: str) -> Path:
    """避免覆盖：同名时追加 (2)(3)…"""
    directory.mkdir(parents=True, exist_ok=True)
    candidate = directory / f"{stem}.{ext}"
    n = 2
    while candidate.exists():
        candidate = directory / f"{stem} ({n}).{ext}"
        n += 1
    return candidate
