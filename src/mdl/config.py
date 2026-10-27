"""配置：路径、并发、Cookie、代理。

全部支持环境变量覆盖，方便在 CI / Docker / 本地之间切换。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass
class Settings:
    """运行期配置。"""

    download_dir: Path = field(
        default_factory=lambda: Path(_env("MDL_DOWNLOAD_DIR") or "downloads")
    )
    concurrency: int = field(default_factory=lambda: max(1, _env_int("MDL_CONCURRENCY", 3)))
    timeout: float = 20.0
    retries: int = 3
    proxy: str = field(default_factory=lambda: _env("MDL_PROXY"))
    user_agent: str = DEFAULT_UA

    # 命名模板：{artist} {title} {album} {index}
    filename_template: str = field(
        default_factory=lambda: _env("MDL_FILENAME_TEMPLATE") or "{artist} - {title}"
    )
    # 子目录模板，留空则直接放在 download_dir
    subdir_template: str = field(default_factory=lambda: _env("MDL_SUBDIR_TEMPLATE"))

    embed_cover: bool = True
    embed_lyric: bool = True
    save_lyric_file: bool = False

    # 各平台 Cookie（登录后可拿到更高音质 / 更多版权）
    cookies: dict[str, str] = field(
        default_factory=lambda: {
            "netease": _env("MDL_NETEASE_COOKIE", "os=pc; appver=2.9.7"),
            "qq": _env("MDL_QQ_COOKIE"),
            "kugou": _env("MDL_KUGOU_COOKIE"),
            "kuwo": _env("MDL_KUWO_COOKIE"),
        }
    )

    def cookie_for(self, provider: str) -> str:
        return self.cookies.get(provider, "")


settings = Settings()


def cors_origins() -> list[str]:
    """允许跨域的前端来源。

    GitHub Pages 上的静态前端需要一个后端地址，浏览器会带 Origin 过来，
    所以默认放开 Pages 域名和本地开发端口。用 MDL_CORS_ORIGINS 覆盖，
    逗号分隔，或设为 "*" 允许任意来源。
    """
    raw = _env("MDL_CORS_ORIGINS")
    if raw:
        return [o.strip() for o in raw.split(",") if o.strip()]
    return [
        "https://p4rfa1t.github.io",
        "http://127.0.0.1:8000",
        "http://localhost:8000",
        "http://127.0.0.1:8765",
        "http://localhost:8765",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]
