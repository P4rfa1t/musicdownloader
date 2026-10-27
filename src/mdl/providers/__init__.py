"""Provider 注册表 + 链接自动识别。"""

from __future__ import annotations

import re
from typing import Iterable

from ..http import Http
from ..models import Target
from .base import Provider, StreamUnavailable, strip_tags
from .kugou import KugouProvider
from .kuwo import KuwoProvider
from .netease import NeteaseProvider
from .qq import QQProvider

PROVIDER_CLASSES: dict[str, type[Provider]] = {
    "netease": NeteaseProvider,
    "qq": QQProvider,
    "kugou": KugouProvider,
    "kuwo": KuwoProvider,
}

ALL_PROVIDERS: list[str] = list(PROVIDER_CLASSES)


class Registry:
    """持有所有 provider 实例，按需分发。"""

    def __init__(self, http: Http, only: Iterable[str] | None = None) -> None:
        self.http = http
        names = list(only) if only else ALL_PROVIDERS
        self.providers: dict[str, Provider] = {
            n: PROVIDER_CLASSES[n](http) for n in names if n in PROVIDER_CLASSES
        }

    def get(self, name: str) -> Provider:
        try:
            return self.providers[name]
        except KeyError:
            raise ValueError(f"不支持的平台: {name}") from None

    def names(self) -> list[str]:
        return list(self.providers)

    # ---------- 链接识别 ----------

    # 平台特征 -> provider 名
    _HOST_RULES: list[tuple[re.Pattern[str], str]] = [
        (re.compile(r"(^|\.)music\.163\.com$|(^|\.)163cn\.tv$|y\.music\.163\.com"), "netease"),
        (re.compile(r"(^|\.)y\.qq\.com$|(^|\.)qq\.com$|c\.y\.qq\.com"), "qq"),
        (re.compile(r"(^|\.)kugou\.com$|(^|\.)kgmusic\.cn$"), "kugou"),
        (re.compile(r"(^|\.)kuwo\.cn$"), "kuwo"),
    ]

    def detect(self, text: str) -> Target:
        """把用户输入（链接 / 分享文案 / 纯 id）识别成 Target。"""
        raw = (text or "").strip()
        if not raw:
            raise ValueError("输入为空")

        # 分享文案里夹着 URL 时，先抽 URL
        m = re.search(r"https?://[^\s，,。；;\"'）)]+", raw)
        url = m.group(0) if m else raw

        host = ""
        m2 = re.match(r"https?://([^/:?#]+)", url, re.I)
        if m2:
            host = m2.group(1).lower()

        candidates: list[str] = []
        for pat, name in self._HOST_RULES:
            if host and pat.search(host) and name in self.providers:
                candidates.append(name)
        if not candidates:
            # 没有可识别域名：可能是纯 id 或短链，按所有平台试
            candidates = list(self.providers)

        for name in candidates:
            provider = self.providers[name]
            tid = provider.parse_id(url)
            if tid:
                return Target(provider=name, kind=_kind_of(provider, url), id=tid, raw_url=url)

        raise ValueError(f"无法识别的链接或 id：{raw[:120]}")


def _kind_of(provider: Provider, url: str) -> str:
    low = url.lower()
    if "playlist" in low or "toplist" in low or "discover" in low:
        return "playlist"
    if "album" in low:
        return "album"
    return "song"


__all__ = [
    "ALL_PROVIDERS",
    "PROVIDER_CLASSES",
    "Registry",
    "Provider",
    "StreamUnavailable",
    "strip_tags",
]
