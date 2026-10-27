"""命令行入口：mdl search / dl / batch / serve。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from . import __version__
from .config import settings
from .downloader import Downloader
from .http import Http
from .providers import ALL_PROVIDERS, Registry


def fix_console_encoding() -> None:
    """Windows 控制台默认 GBK 代码页，中文会变乱码。

    切成 65001(UTF-8) 并把标准流设成 UTF-8。`mdl` 命令和打包后的 exe 都走这里。
    """
    if os.name != "nt":
        return
    try:
        os.system("chcp 65001 > nul")
    except Exception:  # noqa: BLE001 - 拿不到控制台就算了，不该拦住主流程
        pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - 被重定向到管道时没有 reconfigure
            pass


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


def cmd_accounts(args: argparse.Namespace) -> int:
    """管理各平台登录凭据，并（可选）列账号歌单。

    凭据存本地 cookies.json，只写不读回，列表里只报长度。
    """
    from .accounts import AccountStore, StoreError

    actions = [a for a in (args.set_cookie, args.clear, args.playlists) if a]
    action = "list" if not actions else ("set" if args.set_cookie else ("clear" if args.clear else "playlists"))

    if action == "set":
        provider = args.set_cookie
        cookie = args.cookie or ""
        if not cookie:
            print(f"从标准输入读取 {provider} 的 Cookie（结束按 Ctrl-Z 回车）：", file=sys.stderr)
            cookie = sys.stdin.read()
        cookie = cookie.strip()
        if not cookie:
            print("Cookie 为空，什么都没做", file=sys.stderr)
            return 1
        try:
            AccountStore().set(provider, cookie)
        except StoreError as exc:
            print(f"保存失败：{exc}", file=sys.stderr)
            return 1
        print(f"[{provider}] 已保存，长度 {len(cookie)}")
        return 0

    if action == "clear":
        provider = args.clear
        try:
            removed = AccountStore().clear(provider)
        except StoreError as exc:
            print(f"清除失败：{exc}", file=sys.stderr)
            return 1
        print(f"[{provider}] {'已清除' if removed else '本来就没有凭据'}")
        return 0

    store = AccountStore()
    names = [args.playlists] if action == "playlists" else list(ALL_PROVIDERS)

    async def run(registry: Registry, _dl: Downloader):
        rc = 0
        for name in names:
            prov = registry.get(name)
            label = getattr(prov, "label", "") or name
            cookie = store.get(name)
            if action == "playlists":
                if not cookie:
                    print(f"[{label}] 未登录，先 `mdl accounts --set {name}` 保存凭据", file=sys.stderr)
                    return 1
                try:
                    playlists = await prov.user_playlists(cookie)
                except Exception as exc:  # noqa: BLE001 - 单个平台失败不影响其他输出
                    print(f"[{label}] 读歌单失败：{exc}", file=sys.stderr)
                    return 1
                if not playlists:
                    print(f"[{label}] 没有读到歌单")
                    continue
                print(f"[{label}] {len(playlists)} 个歌单")
                for i, pl in enumerate(playlists, 1):
                    print(f"{i:>3}  {pl.name[:32]:<34} {pl.track_count:>5} 首  {pl.id}")
                continue

            # list 模式：未登录直接标未登录，有凭据才联网核实
            if not cookie:
                print(f"{label:<8} 未登录（`mdl accounts --set {name}` 添加凭据）")
                continue
            try:
                info = await prov.account_info(cookie)
            except Exception as exc:  # noqa: BLE001 - 凭据失效就是凭据失效，别中断列表
                print(f"{label:<8} 凭据无效：{exc}")
                rc = 1
                continue
            if info is None or not info.logged_in:
                print(f"{label:<8} 凭据未通过验证")
                rc = 1
                continue
            extra = f" VIP {info.level}" if info.vip else ""
            print(f"{label:<8} 已登录 {info.nickname or info.uid}{extra}")
            if prov.playlists_supported:
                try:
                    playlists = await prov.user_playlists(cookie)
                except Exception as exc:  # noqa: BLE001
                    print(f"{'':<8} 歌单读取失败：{exc}")
                    continue
                print(f"{'':<8} 歌单 {len(playlists)} 个")
        return rc

    return asyncio.run(_with_stack(run))


def cmd_serve(args: argparse.Namespace) -> int:
    import threading
    import webbrowser

    from .server import run as serve

    host = args.host
    shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    if args.open:
        url = f"http://{shown}:{args.port}/"
        # 等 uvicorn 起来再开，1.2s 是个经验值
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
        print(f"服务启动后会自动打开 {url}")

    serve(host=host, port=args.port, reload=args.reload)
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

    a = sub.add_parser("accounts", help="管理平台登录凭据、查看账号歌单")
    a.add_argument("--set", dest="set_cookie", metavar="PROVIDER", choices=list(ALL_PROVIDERS),
                   help="保存某平台的 Cookie（不给 --cookie 就从标准输入读）")
    a.add_argument("--cookie", help="配合 --set 一起用，避免交互输入")
    a.add_argument("--clear", metavar="PROVIDER", choices=list(ALL_PROVIDERS), help="清除某平台凭据")
    a.add_argument("--playlists", metavar="PROVIDER", choices=list(ALL_PROVIDERS),
                   help="列出该账号的歌单")
    a.set_defaults(func=cmd_accounts)

    v = sub.add_parser("serve", help="启动 Web 界面")
    v.add_argument("--host", default="127.0.0.1")
    v.add_argument("--port", type=int, default=8000)
    v.add_argument("--reload", action="store_true")
    v.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    v.set_defaults(func=cmd_serve)
    return p


def main(argv: list[str] | None = None) -> int:
    fix_console_encoding()
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
