"""打包入口：不带参数直接启动服务，双击 exe 就能用。

带参数时行为和 `mdl` 命令完全一致（search / dl / serve）。
"""

from __future__ import annotations

import os
import sys

# 绝对导入：PyInstaller 把本文件当 __main__ 执行，相对导入会报
# "attempted relative import with no known parent package"
from mdl.cli import main as cli_main


def _fix_console_encoding() -> None:
    """Windows 控制台默认 GBK 代码页，中文会变乱码。

    切成 65001(UTF-8) 并把标准流设成 UTF-8，日志和搜索结果才看得懂。
    """
    if os.name != "nt":
        return
    try:
        os.system("chcp 65001 > nul")
    except Exception:
        pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main(argv: list[str] | None = None) -> int:
    _fix_console_encoding()
    args = list(sys.argv[1:] if argv is None else argv)
    # 双击 exe（没有参数）时，直接起服务并打开浏览器
    if not args:
        args = ["serve", "--open"]
    return int(cli_main(args))


if __name__ == "__main__":
    raise SystemExit(main())