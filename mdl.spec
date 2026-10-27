# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：把 MusicDL 后端编译成可分发程序。

用法：
    .venv\\Scripts\\pyinstaller.exe mdl.spec --noconfirm

产物：
    dist\\mdl\\mdl.exe          主程序
    dist\\mdl\\_internal\\      依赖与前端静态文件（必须和 exe 一起分发）
    dist\\mdl-<ver>-win64.zip  打包好的压缩包

为什么是「目录模式」而不是单文件：
    本机 Python 3.14.6 + PyInstaller 6.22.3 的组合下，`--onefile` 的
    bootloader 一定会报 "Could not create temporary directory!"（用 hello
    world 最小样例复现过，onedir 正常）。这是工具链的问题，不是项目代码。
    目录模式启动还更快，免去每次解压到 %TEMP%，对常驻服务更合适。

打包完的目录可以直接拷走运行，不需要装 Python。
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

ROOT = Path(SPECPATH)  # noqa: F821 - 由 PyInstaller 注入
WEB = ROOT / "src" / "mdl" / "web"

# uvicorn 按字符串选实现类，静态分析看不到，必须显式声明
hiddenimports = [
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    "anyio",
    "anyio._backends",
    "anyio._backends._asyncio",
    "h11",
    "sniffio",
    "click",
    "certifi",
    "mdl.player",
    "mdl.app_window",
]

datas = [(str(WEB), "mdl/web")]
# httpx 走 TLS 需要 certifi 的 cacert.pem
for item in collect_data_files("certifi"):
    datas.append((item[0], item[1]))

a = Analysis(  # noqa: F821
    [str(ROOT / "src" / "mdl" / "entry.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "unittest",
        "pytest",
        "pydoc",
        "doctest",
        "lib2to3",
        "test",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="mdl",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX 容易被杀软误报
    # 双击就是一个窗口应用，不留黑框；日志写在 %APPDATA%\MusicDL\mdl.log
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="mdl",
)