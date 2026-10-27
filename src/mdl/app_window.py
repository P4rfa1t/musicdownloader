"""把 MusicDL 跑成一个窗口应用，而不是「终端 + 浏览器标签」。

实现思路：不引入 Electron / Tauri 这类重型外壳，直接用系统自带的 Chromium
内核浏览器开「应用窗口」（Edge/Chrome 的 --app= 模式）。这样得到的是：
没有地址栏、没有标签页、独立任务栏图标与窗口尺寸，双击即用，且零额外依赖。

关窗即退出的判定不用进程 PID（Chromium 的启动进程常常立刻退出，PID 不可靠），
改用前端心跳：页面每 5 秒打一次 /api/ping，超过 idle_timeout 没有心跳就认为
窗口已经关了，主动停掉 uvicorn。
"""

from __future__ import annotations

import logging
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from pathlib import Path

log = logging.getLogger("mdl.app")

# 窗口尺寸：1280x880 在 1080p 上是「铺满但不全屏」的舒适区
WINDOW_SIZE = "1280,880"

# 首次打开后给页面的加载宽限；之后按心跳判断窗口是否还活着
STARTUP_GRACE = 60.0


def _candidates() -> list[Path]:
    """按「越正常越靠前」的顺序列出候选数据目录。

    %LOCALAPPDATA% 在部分机器上被组策略 / 安全软件收紧了写权限（实测本机
    直接 mkdir 报 WinError 5），所以这里不写死单一路径，逐个试到能写为止。
    """
    home = Path.home()
    if sys.platform == "win32":
        appdata = Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming")
        local = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
        bases = [appdata, local, home]
    elif sys.platform == "darwin":
        bases = [home / "Library" / "Application Support", home]
    else:
        bases = [Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share"), home]
    out = [b / "MusicDL" for b in bases]
    # 便携场景：程序旁边的目录，或者干脆当前目录（免安装 U 盘、受限账户都能落）
    out.append(_app_dir() / "MusicDL")
    out.append(Path.cwd() / "MusicDL")
    out.append(Path(tempfile.gettempdir()) / "MusicDL")
    return out


def _app_dir() -> Path:
    """程序所在目录：打包后是 exe 旁边，源码运行时是项目根。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


_cached_dir: Path | None = None


def data_dir() -> Path:
    """用户级数据目录（日志、窗口配置）：第一个能建起来的候选。"""
    global _cached_dir
    if _cached_dir is not None and _cached_dir.is_dir():
        return _cached_dir
    for path in _candidates():
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError:
            continue
        if os.access(path, os.W_OK):
            _cached_dir = path
            log.debug("数据目录: %s", path)
            return path
    # 实在都不行就用临时目录，至少还能跑
    fallback = Path(tempfile.gettempdir()) / "MusicDL"
    fallback.mkdir(parents=True, exist_ok=True)
    _cached_dir = fallback
    return fallback


def profile_dir() -> Path:
    """独立浏览器配置目录：跟用户日常浏览的窗口彻底隔离。"""
    path = data_dir() / "window"
    path.mkdir(parents=True, exist_ok=True)
    return path


def free_port(preferred: int = 8000, tries: int = 12) -> int:
    """从 preferred 开始找一个能绑上的端口。"""
    for offset in range(tries):
        port = preferred + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    # 都被占了就让系统挑一个
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def find_browser() -> str | None:
    """找一个支持 --app 模式的 Chromium 内核浏览器。"""
    candidates: list[str] = []
    if sys.platform == "win32":
        roots = [
            os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
            os.environ.get("PROGRAMFILES", r"C:\Program Files"),
            os.environ.get("LOCALAPPDATA", ""),
        ]
        rels = [
            r"Microsoft\Edge\Application\msedge.exe",
            r"Google\Chrome\Application\chrome.exe",
            r"Chromium\Application\chrome.exe",
            r"BraveSoftware\Brave-Browser\Application\brave.exe",
            r"Vivaldi\Application\vivaldi.exe",
        ]
        for root in roots:
            if not root:
                continue
            for rel in rels:
                candidates.append(os.path.join(root, rel))
    elif sys.platform == "darwin":
        candidates += [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ]
    else:
        for name in ("google-chrome", "chromium", "chromium-browser", "microsoft-edge"):
            found = _which(name)
            if found:
                candidates.append(found)

    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return None


def _which(name: str) -> str | None:
    from shutil import which

    return which(name)


def _window_args(exe: str, url: str, extra: list[str]) -> list[str]:
    return [
        exe,
        f"--app={url}",
        f"--user-data-dir={profile_dir()}",
        f"--window-size={WINDOW_SIZE}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate,MediaRouter,OptimizationHints",
        # 窗口在后台时别把计时器/渲染节流掉，否则心跳与播放进度都会失准
        "--disable-background-timer-throttling",
        "--disable-renderer-backgrounding",
        *extra,
    ]


def _spawn(args: list[str]) -> subprocess.Popen | None:
    try:
        creationflags = 0
        if sys.platform == "win32":
            # 别让子进程跟着控制台一起被 Ctrl+C 打断
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        return subprocess.Popen(  # noqa: S603 - 参数全由本模块拼，无外部输入
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
    except OSError as exc:
        log.warning("启动窗口失败：%s", exc)
        return None


def launch_window(url: str) -> subprocess.Popen | None:
    """开一个无地址栏的应用窗口；找不到浏览器返回 None。

    某些受限环境（容器、被策略收紧了令牌的父进程）里 Chromium 自带的沙箱
    起不来，进程会立刻带 STATUS_BREAKPOINT 退出。这时用 --no-sandbox 重试
    一次——它只影响我们这个独立 profile 的实例，不影响用户日常浏览器。
    """
    exe = find_browser()
    if not exe:
        log.warning("没找到 Edge/Chrome，退回默认浏览器")
        return None

    for extra in ([], ["--no-sandbox"]):
        proc = _spawn(_window_args(exe, url, extra))
        if proc is None:
            continue
        # 给它 3 秒：能活下来就说明窗口起来了
        for _ in range(12):
            if proc.poll() is not None:
                break
            time.sleep(0.25)
        if proc.poll() is None:
            log.info("已启动窗口：%s%s", exe, "（无沙箱模式）" if extra else "")
            return proc
        log.warning("窗口进程退出（code=%s），%s", proc.returncode,
                    "改用无沙箱模式重试" if not extra else "放弃自建窗口")
    return None


def _wait_ready(port: int, timeout: float = 25.0) -> bool:
    """等 uvicorn 开始监听。"""
    import json
    import urllib.request

    deadline = time.time() + timeout
    url = f"http://127.0.0.1:{port}/api/health"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as resp:  # noqa: S310 - 本机地址
                if json.loads(resp.read().decode("utf-8")).get("ok"):
                    return True
        except Exception:  # noqa: BLE001 - 还没起来就再等等
            time.sleep(0.25)
    return False


def run(
    host: str = "127.0.0.1",
    port: int = 0,
    open_window: bool = True,
    idle_timeout: float = 45.0,
) -> int:
    """启动服务 + 窗口，窗口关掉（心跳停止）后一起退出。"""
    import uvicorn

    from . import __version__
    from .server import app, heartbeat

    if not port:
        port = free_port(8000)
    url = f"http://127.0.0.1:{port}/"

    config = uvicorn.Config(app, host=host, port=port, log_config=None, access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="uvicorn", daemon=True)
    thread.start()

    if not _wait_ready(port):
        log.error("服务没能在预期时间内就绪，端口 %s", port)
        server.should_exit = True
        thread.join(timeout=5)
        return 1

    print(f"MusicDL {__version__} 已启动：{url}")
    print("关掉窗口就会自动退出（也可以在这里按 Ctrl+C）。")

    if open_window:
        if launch_window(url) is None:
            webbrowser.open(url)

    opened = open_window
    try:
        while thread.is_alive():
            time.sleep(1.0)
            if not heartbeat.alive():
                # 页面还没连上：先等 STARTUP_GRACE，别把还在启动的窗口当成「已关闭」
                waited = time.monotonic() - (heartbeat.started_at or time.monotonic())
                if waited > STARTUP_GRACE:
                    if opened:
                        # 窗口开出来了但页面没连上（浏览器被拦、profile 被占用等），
                        # 退回系统默认浏览器再给一次机会，别让用户看到「双击没反应」
                        log.warning("窗口没有连上页面，改用默认浏览器打开 %s", url)
                        webbrowser.open(url)
                        opened = False
                        heartbeat.started_at = time.monotonic()
                        continue
                    log.info("页面一直没有连上，退出")
                    break
                continue
            if not opened and heartbeat.alive():
                log.info("页面已通过默认浏览器连上")
                opened = True
            if heartbeat.idle() > idle_timeout:
                log.info("窗口已关闭，服务退出")
                break
    except KeyboardInterrupt:
        print("\n收到中断，正在退出…")

    server.should_exit = True
    thread.join(timeout=10)
    return 0


def run_and_exit(**kwargs) -> int:
    """给 CLI 用的薄包装：异常统一落日志，不让双击闪退。"""
    code = 1
    try:
        code = run(**kwargs)
    except Exception as exc:  # noqa: BLE001
        log.exception("窗口模式启动失败")
        _fatal(f"启动失败：{exc}")
    return code


def _fatal(message: str) -> None:
    """没有控制台时（打包成窗口程序）用一个系统对话框兜底。"""
    print(message, file=sys.stderr)
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, message, "MusicDL", 0x10)
        except Exception:  # noqa: BLE001
            pass


__all__ = ["run", "run_and_exit", "launch_window", "find_browser", "free_port", "data_dir"]