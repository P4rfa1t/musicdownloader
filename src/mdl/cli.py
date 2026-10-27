"""命令行入口：mdl search / dl / batch / serve。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from . import __version__
from .config import settings
from .downloader import Downloader
from .http import Http
from .providers import ALL_PROVIDERS, Registry


async def _with_stack(coro_factory):
    http = Http(settings.cookies)
    await http.open()
    try:
        registry = Registry(http)
        downloader = Downloader(registry, http)
        return await coro_factory(registry, downloader)
    finally:
        await http.close()


def cmd_search(args: argparse.Namespace) -> int:
    async def run(registry: Registry, _dl: Downloader):
        names = registry.names() if args.provider == "all" else [args.provider]
        rows = []
        for name in names:
            try:
                tracks = await registry.get(name).search(args.keyword, args.limit)
            except Exception as exc:
                print(f"[{name}] 搜索失败: {exc}", file=sys.stderr)
                continue
            for t in tracks:
                rows.append(t)
        if args.json:
            print(json.dumps([t.to_dict() for t in rows], ensure_ascii=False, indent=2))
            return 0
        if not rows:
            print("没有结果")
            return 1
        print(f"{'#':>3}  {'平台':8} {'标题':<32} {'歌手':<20} {'时长':>6}  id")
        for i, t in enumerate(rows, 1):
            title = t.title[:30]
            artist = t.artist_text[:18]
            print(f"{i:>3}  {t.provider:8} {title:<32} {artist:<20} {t.duration_text:>6}  {t.id}")
        return 0

    return asyncio.run(_with_stack(run))


def cmd_dl(args: argparse.Namespace) -> int:
    async def run(registry: Registry, downloader: Downloader):
        if args.url:
            target = registry.detect(args.url)
            result = await registry.get(target.provider).resolve_url(args.url)
            tracks = result.tracks
            print(f"解析到《{result.title}》 {len(tracks)} 首")
        else:
            name = args.provider
            if name == "all":
                raise SystemExit("下载时必须指定 --provider")
            prov = registry.get(name)
            if args.id:
                tracks = [await prov.track_from_id(args.id)]
            elif args.keyword:
                found = await prov.search(args.keyword, 1)
                if not found:
                    raise SystemExit("没有搜索到结果")
                tracks = found[:1]
            else:
                raise SystemExit("需要 --url / --id / --keyword 之一")

        sem = asyncio.Semaphore(max(1, settings.concurrency))
        failed = 0

        async def one(idx: int, track):
            nonlocal failed
            async with sem:
                res = await downloader.download(track, args.quality, index=idx + 1)
                if res.ok:
                    print(f"✓ {res.title}  ->  {res.path}  ({res.size // 1024} KB, {res.quality})")
                else:
                    failed += 1
                    print(f"✗ {res.title}  {res.error}", file=sys.stderr)

        await asyncio.gather(*(one(i, t) for i, t in enumerate(tracks)))
        print(f"完成：成功 {len(tracks) - failed} / 共 {len(tracks)}，目录 {Path(settings.download_dir).resolve()}")
        return 0 if failed == 0 else 2

    return asyncio.run(_with_stack(run))


def cmd_serve(args: argparse.Namespace) -> int:
    from .server import run as serve

    serve(host=args.host, port=args.port, reload=args.reload)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mdl", description="MusicDL —— 多平台音乐解析下载器")
    p.add_argument("-V", "--version", action="version", version=f"mdl {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search", help="搜索歌曲")
    s.add_argument("keyword")
    s.add_argument("-p", "--provider", default="all", choices=["all", *ALL_PROVIDERS])
    s.add_argument("-n", "--limit", type=int, default=10)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_search)

    d = sub.add_parser("dl", help="下载（链接 / id / 关键词）")
    d.add_argument("url", nargs="?", help="分享链接或分享文案")
    d.add_argument("-p", "--provider", default="all", choices=["all", *ALL_PROVIDERS])
    d.add_argument("-i", "--id", help="平台内 id")
    d.add_argument("-k", "--keyword", help="按关键词取第一条结果")
    d.add_argument("-q", "--quality", default="320", choices=["128", "192", "320", "flac"])
    d.set_defaults(func=cmd_dl)

    v = sub.add_parser("serve", help="启动 Web 界面")
    v.add_argument("--host", default="127.0.0.1")
    v.add_argument("--port", type=int, default=8000)
    v.add_argument("--reload", action="store_true")
    v.set_defaults(func=cmd_serve)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
