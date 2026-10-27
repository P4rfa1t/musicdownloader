"""把 src/mdl/web 同步到 docs/，供 GitHub Pages 直接托管。

GitHub Pages 只能给静态文件，没有 Python 进程。所以 Pages 上跑的是
同一个前端，只是它需要一个后端地址（用户自己跑 mdl serve，或部署
一个 CORS 中继 Worker）。

用法::

    python scripts/build_pages.py                    # 生成 docs/
    python scripts/build_pages.py --api https://x.dev # 预置后端地址

生成的 docs/index.html 与源文件一致，只是多了（可选的）
``window.MDL_API_BASE`` 注入和一份 Pages 用的 404 兜底页。
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "mdl" / "web"
OUT = ROOT / "docs"

FILES = ["index.html", "app.css", "app.js", "direct.js"]

# 构建产物标记，避免把 docs/ 误当成源码目录
BANNER = "<!-- 由 scripts/build_pages.py 生成，请改 src/mdl/web/ 下的源文件 -->"

INJECT = """  <script>
    // 由构建脚本注入：Pages 上没有同源后端，需要一个绝对地址
    window.MDL_API_BASE = {api!r};
  </script>
"""


def build(api: str = "", clean: bool = True) -> Path:
    if not SRC.is_dir():
        raise SystemExit(f"找不到源目录: {SRC}")
    missing = [f for f in FILES if not (SRC / f).is_file()]
    if missing:
        raise SystemExit(f"源目录缺少文件: {', '.join(missing)}")

    if clean and OUT.exists():
        for f in FILES:
            (OUT / f).unlink(missing_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)

    for name in FILES:
        shutil.copy2(SRC / name, OUT / name)

    html = (OUT / "index.html").read_text(encoding="utf-8")
    if api:
        # 注入到 </head> 之前，保证 app.js 执行时已经就位
        html = html.replace("</head>", INJECT.format(api=api) + "</head>", 1)
    if BANNER not in html:
        html = html.replace("<!DOCTYPE html>", f"<!DOCTYPE html>\n{BANNER}", 1)
    (OUT / "index.html").write_text(html, encoding="utf-8")

    # Pages 的 404 兜底：任何未知路径都回到首页
    (OUT / "404.html").write_text(
        '<!DOCTYPE html><meta charset="utf-8">'
        '<meta http-equiv="refresh" content="0; url=./">'
        '<title>MusicDL</title><a href="./">回到 MusicDL</a>',
        encoding="utf-8",
    )
    (OUT / ".nojekyll").write_text("", encoding="utf-8")

    return OUT


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="生成 GitHub Pages 静态站点")
    p.add_argument("--api", default="", help="预置后端地址，如 https://mdl.example.workers.dev")
    p.add_argument("--keep", action="store_true", help="不清理已有产物")
    args = p.parse_args(argv)

    out = build(api=args.api.strip(), clean=not args.keep)
    total = sum(f.stat().st_size for f in out.iterdir() if f.is_file())
    print(f"已生成 {out}")
    for f in sorted(out.iterdir()):
        if f.is_file():
            print(f"  {f.name:16s} {f.stat().st_size:>8,d} B")
    print(f"合计 {total:,d} B")
    if not args.api:
        print("提示：没注入 API 地址，Pages 打开后需要点「⚙ 设置」填后端。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
