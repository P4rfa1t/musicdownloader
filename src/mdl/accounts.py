"""账号凭据存储：把各平台的 Cookie 落在本地一个 JSON 文件里。

为什么不放浏览器 localStorage：凭据要参与服务端请求（签名、Referer、去水印），
浏览器拿着没用；放服务端还有个好处——换浏览器/换设备只要还连同一个后端就不用重登。

安全边界写清楚：文件是明文 JSON，权限按当前用户可读写（POSIX 下 0600）。
这工具本来就是单机自用，等同浏览器里那堆 Cookie；但它是明文落盘，
所以默认放在用户目录下的 .musicdl 里，不进仓库、不进下载目录。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from .config import settings

log = logging.getLogger("mdl.accounts")


class StoreError(RuntimeError):
    """凭据落盘失败。带上路径，让界面能告诉用户到底是哪个目录不可写。"""

    def __init__(self, path: Path, cause: OSError) -> None:
        self.path = path
        self.cause = cause
        super().__init__(f"无法写入凭据文件 {path}：{cause}")


class AccountStore:
    """provider -> cookie 的简单键值存储，写盘用「临时文件 + 替换」保证原子性。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path or settings.account_file).expanduser()
        self._data: dict[str, str] = {}
        self._load()

    # ------------------------------------------------------------------ 读写

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8") or "{}")
        except (OSError, ValueError) as exc:
            # 文件坏了不该让整个服务起不来，只是账号需要重登
            log.warning("账号文件读取失败（%s），按空处理：%s", self.path, exc)
            return
        if isinstance(raw, dict):
            self._data = {
                str(k): str(v) for k, v in raw.items() if isinstance(v, str) and v.strip()
            }

    def _save(self) -> None:
        payload = json.dumps(self._data, ensure_ascii=False, indent=2, sort_keys=True)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(
                dir=str(self.path.parent), prefix=".accounts-", suffix=".tmp"
            )
        except OSError as exc:
            raise StoreError(self.path, exc) from exc
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            if os.name == "posix":
                os.chmod(tmp, 0o600)  # 明文凭据，别让同机其他用户读
            os.replace(tmp, self.path)
        except OSError as exc:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise StoreError(self.path, exc) from exc
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ------------------------------------------------------------------ 对外

    def get(self, provider: str) -> str:
        return self._data.get(provider, "")

    def has(self, provider: str) -> bool:
        return bool(self._data.get(provider))

    def set(self, provider: str, cookie: str) -> None:
        cookie = (cookie or "").strip()
        if not cookie:
            self.clear(provider)
            return
        self._data[provider] = cookie
        self._save()

    def clear(self, provider: str) -> bool:
        if provider not in self._data:
            return False
        del self._data[provider]
        self._save()
        return True

    def all(self) -> dict[str, str]:
        return dict(self._data)

    def describe(self) -> dict[str, Any]:
        """只暴露「有没有」，绝不回显 Cookie 本身。"""
        return {k: {"has_cookie": True, "length": len(v)} for k, v in self._data.items()}