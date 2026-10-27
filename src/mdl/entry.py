"""打包入口：不带参数直接启动服务，双击 exe 就能用。

带参数时行为和 `mdl` 命令完全一致（search / dl / serve）。
"""

from __future__ import annotations

import sys

# 绝对导入：PyInstaller 把本文件当 __main__ 执行，相对导入会报
# "attempted relative import with no known parent package"
from mdl.cli import fix_console_encoding, main as cli_main


def main(argv: list[str] | None = None) -> int:
    fix_console_encoding()
    args = list(sys.argv[1:] if argv is None else argv)
    # 双击 exe（没有参数）时，直接起服务并打开浏览器
    if not args:
        args = ["serve", "--open"]
    return int(cli_main(args))


if __name__ == "__main__":
    raise SystemExit(main())