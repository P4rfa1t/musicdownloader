"""打包入口：不带参数直接以窗口模式启动，双击 exe 就是一个应用。

带参数时行为和 `mdl` 命令完全一致（search / dl / accounts / serve / app）。
"""

from __future__ import annotations

import sys

# 绝对导入：PyInstaller 把本文件当 __main__ 执行，相对导入会报
# "attempted relative import with no known parent package"
from mdl.cli import fix_console_encoding, main as cli_main


def ensure_streams() -> None:
    """窗口版 exe（无控制台）里 sys.stdout / stderr 是 None，print 会炸。

    从命令行启动时这两个流是有效的，保持原样；双击启动时把输出接到日志文件，
    既不丢信息，也不会因为一次 print 把整个进程送走。
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        from mdl.app_window import data_dir

        path = data_dir() / "stdout.log"
        stream = open(path, "a", encoding="utf-8", errors="replace")  # noqa: SIM115 - 进程级出口
    except Exception:  # noqa: BLE001 - 连日志都开不了就静默兜住
        class _Sink:
            def write(self, _data: str) -> int:
                return 0

            def flush(self) -> None:
                return None

        stream = _Sink()  # type: ignore[assignment]
    if sys.stdout is None:
        sys.stdout = stream  # type: ignore[assignment]
    if sys.stderr is None:
        sys.stderr = stream  # type: ignore[assignment]


def main(argv: list[str] | None = None) -> int:
    ensure_streams()
    fix_console_encoding()
    args = list(sys.argv[1:] if argv is None else argv)
    # 双击 exe（没有参数）时起服务并开一个应用窗口，而不是丢个浏览器标签出去
    if not args:
        args = ["app"]
    return int(cli_main(args))


if __name__ == "__main__":
    raise SystemExit(main())