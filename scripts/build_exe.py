"""把后端编译成免装 Python 的可执行程序。

    python scripts/build_exe.py            # 编译 + 打 zip
    python scripts/build_exe.py --no-zip   # 只编译

产物在 dist/mdl/：mdl.exe 加 _internal/ 目录，整个文件夹拷走就能跑。

为什么不是单文件 exe：本机 Python 3.14.6 + PyInstaller 6.22.3 的组合下
--onefile 的 bootloader 必定报 "Could not create temporary directory!"，
用 hello world 最小样例就能复现，onedir 正常。这是工具链的坑，不是项目代码。
目录模式还有个好处：启动不用每次解压到 %TEMP%，服务型程序更合适。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "mdl.spec"
DIST = ROOT / "dist" / "mdl"
BUILD = ROOT / "build"

# Windows 控制台默认 GBK，中文进度提示会乱码
if sys.platform == "win32":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def run(cmd: list[str]) -> None:
    print("+", " ".join(cmd))
    proc = subprocess.run(cmd, cwd=ROOT)
    if proc.returncode != 0:
        raise SystemExit(f"命令失败（退出码 {proc.returncode}）")


def clean() -> None:
    for path in (DIST.parent, BUILD):
        if path.exists():
            print(f"清理 {path.relative_to(ROOT)}")
            shutil.rmtree(path, ignore_errors=True)


def build() -> Path:
    run([sys.executable, "-m", "PyInstaller", str(SPEC), "--noconfirm", "--log-level", "WARN"])
    exe = DIST / ("mdl.exe" if sys.platform == "win32" else "mdl")
    if not exe.is_file():
        raise SystemExit(f"编译产物不存在: {exe}")
    return exe


def smoke_test(exe: Path) -> None:
    """编译完立刻验一次，避免交付一个跑不起来的包。"""
    print("冒烟测试:", exe.name, "--version")
    out = subprocess.run([str(exe), "--version"], capture_output=True, text=True, timeout=120)
    text = (out.stdout or "") + (out.stderr or "")
    if out.returncode != 0 or "mdl" not in text.lower():
        raise SystemExit(f"exe 跑不起来：\n{text[:800]}")
    print("  ->", text.strip().splitlines()[-1] if text.strip() else "(无输出)")

    # 静态资源必须打进去，否则 exe 起来只有 404
    internal = DIST / "_internal" / "mdl" / "web"
    for name in ("index.html", "app.js", "app.css", "account.js", "account.css",
                 "player.js", "player.css"):
        if not (internal / name).is_file():
            raise SystemExit(f"缺少前端资源: {internal / name}")
    print(f"  -> 前端资源完整（{len(list(internal.glob('*')))} 个文件）")

    # 真起一次服务，确认路由与播放模块都在（打包漏 hiddenimport 时这里会崩）
    print("冒烟测试: serve /api/health + /api/ping + /player.js")
    port = 8847
    proc = subprocess.Popen(
        [str(exe), "serve", "--port", str(port), "--host", "127.0.0.1"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.time() + 60
        ok = False
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1) as r:
                    if json.loads(r.read().decode("utf-8")).get("ok"):
                        ok = True
                        break
            except Exception:
                time.sleep(0.3)
        if not ok:
            raise SystemExit("exe 起服务失败（/api/health 不通）")
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=5) as r:
            assert json.loads(r.read().decode("utf-8"))["ok"] is True
        for asset in ("player.js", "player.css"):
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/{asset}", timeout=5) as r:
                if r.status != 200 or not r.read():
                    raise SystemExit(f"静态资源有问题: {asset}")
        print("  -> 服务、心跳、播放器资源全部正常")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()


def make_zip(version: str) -> Path:
    tag = f"{sys.platform.replace('win32', 'win64')}"
    out = DIST.parent / f"mdl-{version}-{tag}.zip"
    print("打包:", out.name)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(DIST.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(DIST.parent))
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="编译 MusicDL 后端")
    p.add_argument("--no-zip", action="store_true", help="不生成 zip")
    p.add_argument("--keep", action="store_true", help="保留上次构建目录")
    args = p.parse_args(argv)

    sys.path.insert(0, str(ROOT / "src"))
    from mdl import __version__

    if not args.keep:
        clean()

    exe = build()
    smoke_test(exe)

    size = sum(f.stat().st_size for f in DIST.rglob("*") if f.is_file())
    print(f"\n产物目录: {DIST}")
    print(f"体积: {size / 1048576:.1f} MB（{len([f for f in DIST.rglob('*') if f.is_file()])} 个文件）")

    if not args.no_zip:
        zpath = make_zip(__version__)
        print(f"压缩包: {zpath} ({zpath.stat().st_size / 1048576:.1f} MB)")
    print("\n用法: 双击 mdl.exe 就是一个窗口应用；或用 mdl.exe search/dl/serve 当命令行用。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())