"""FastAPI 应用：搜索 / 解析 / 下载 / 批量任务 / 静态页面。"""

from __future__ import annotations

import dataclasses
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__
from .accounts import AccountStore, StoreError
from .config import cors_origins, settings
from .downloader import Downloader, JobManager
from .http import Http, HttpError
from .models import QUALITIES, AccountInfo, Track
from .player import PlayService, proxy
from .providers import ALL_PROVIDERS, Registry
from .providers.base import AccountError, StreamUnavailable

log = logging.getLogger("mdl.server")

WEB_DIR = Path(__file__).parent / "web"

# 这些异常从 provider 抛出来时统一转 400/502
PROVIDER_ERRORS = (ValueError, HttpError, StreamUnavailable)


class AppState:
    http: Http
    registry: Registry
    downloader: Downloader
    jobs: JobManager
    accounts: AccountStore
    play: PlayService


state = AppState()


class Heartbeat:
    """窗口模式用的活性信号。

    页面每 5 秒打一次 /api/ping；把最后一次心跳时间记下来，`mdl app` 就能判断
    窗口是不是已经关了——比盯浏览器进程 PID 可靠得多（Chromium 的启动进程常常
    立刻退出，实际渲染进程另算）。
    """

    def __init__(self) -> None:
        self._last = 0.0
        # 进程启动时刻：窗口模式用它给「还没连上」留宽限
        self.started_at = time.monotonic()

    def ping(self) -> None:
        self._last = time.monotonic()

    def alive(self) -> bool:
        return self._last > 0.0

    def never_pinged(self) -> bool:
        return self._last <= 0.0

    def idle(self) -> float:
        if self._last <= 0.0:
            return 0.0
        return time.monotonic() - self._last


heartbeat = Heartbeat()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # 先把面板里存过的 Cookie 叠到配置之上，再建 Http，这样首个请求就带登录态
    store = AccountStore()
    base_cookies = dict(settings.cookies)
    base_cookies.update(store.all())
    http = Http(base_cookies)
    await http.open()
    state.accounts = store
    state.http = http
    state.registry = Registry(http)
    state.downloader = Downloader(state.registry, http)
    state.jobs = JobManager(state.downloader)
    state.play = PlayService(state.registry, http)
    log.info("MusicDL 就绪，下载目录 %s", Path(settings.download_dir).resolve())
    if store.all():
        log.info("已载入账号凭据：%s", ", ".join(sorted(store.all())))
    try:
        yield
    finally:
        await state.jobs.shutdown()
        await http.close()


app = FastAPI(title="MusicDL", version=__version__, lifespan=lifespan)

# 前端现在由本服务直接托管，跨域只留给本机其他端口的预览 / 调试。
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins(),
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?",
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition", "Content-Length"],
)


# ------------------------------------------------------------------ 请求体


class ResolveBody(BaseModel):
    url: str = Field(..., min_length=1, description="分享链接、分享文案或纯 id")


class TrackBody(BaseModel):
    provider: str
    id: str
    title: str = ""
    artists: list[str] = Field(default_factory=list)
    album: str = ""
    duration_ms: int = 0
    cover_url: str = ""
    source_url: str = ""
    extra: dict[str, Any] = Field(default_factory=dict)

    def to_track(self) -> Track:
        return Track(
            provider=self.provider,
            id=self.id,
            title=self.title or f"{self.provider}-{self.id}",
            artists=list(self.artists),
            album=self.album,
            duration_ms=self.duration_ms,
            cover_url=self.cover_url,
            source_url=self.source_url,
            extra=dict(self.extra),
        )


class DownloadBody(TrackBody):
    quality: str = "320"


class BatchBody(BaseModel):
    quality: str = "320"
    tracks: list[TrackBody] = Field(..., min_length=1, max_length=200)


class LoginBody(BaseModel):
    """平台名走路径参数，请求体只放凭据。"""

    cookie: str = Field("", description="整条 Cookie；QQ 可以直接填 QQ 号")


# ------------------------------------------------------------------ 基础接口


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "version": __version__,
        "providers": state.registry.names() if hasattr(state, "registry") else ALL_PROVIDERS,
        # 平台显示名也一起给，前端不用自己维护一份 id → 中文的映射
        "provider_labels": _provider_labels(),
        "download_dir": str(Path(settings.download_dir).resolve()),
    }


@app.get("/api/qualities")
async def qualities() -> dict[str, Any]:
    return {"qualities": QUALITIES, "default": "320"}


@app.get("/api/ping")
async def ping() -> dict[str, Any]:
    """前端心跳：窗口模式靠它判断页面是否还在，普通 serve 模式下无害。"""
    heartbeat.ping()
    return {"ok": True}


# ------------------------------------------------------------------ 账号


# 前端 account.js 是围绕这套契约写的，字段名照抄不要改：
#   GET    /api/accounts                       -> {ok, accounts:[...]}
#   POST   /api/accounts/{provider} {cookie}   -> {ok, account}
#   DELETE /api/accounts/{provider}            -> {ok}
#   POST   /api/accounts/{provider}/qr         -> {ok, key, qr_image, expires_in}
#   GET    /api/accounts/{provider}/qr/{key}   -> {ok, status, message, account?}
#   GET    /api/accounts/{provider}/playlists  -> {ok, playlists:[...], note}
#   GET    /api/playlists/{provider}/{id}      -> {ok, title, tracks:[...]}

QR_STATUSES = ("wait", "scanned", "ok", "expired", "error")


def _exc_detail(exc: BaseException) -> str:
    """把异常翻成一句用户能看懂的话，带类型名方便排查。"""
    text = str(exc).strip()
    name = type(exc).__name__
    return f"{name}: {text}" if text else name


def _provider_or_400(provider: str):
    if provider not in state.registry.providers:
        raise HTTPException(status_code=400, detail=f"不支持的平台: {provider}")
    return state.registry.get(provider)


def _provider_labels() -> dict[str, str]:
    labels: dict[str, str] = {}
    names = state.registry.names() if hasattr(state, "registry") else ALL_PROVIDERS
    for name in names:
        try:
            prov = state.registry.get(name)
        except Exception:  # noqa: BLE001 - 缺一个平台不影响 /api/health
            continue
        labels[name] = getattr(prov, "label", "") or name
    return labels


def _account_payload(provider: str, info, cookie: str) -> dict[str, Any]:
    """把 provider 的身份信息 + 面板需要的静态能力拼成前端认识的那张卡。"""
    prov = state.registry.get(provider)
    payload: dict[str, Any] = {
        "provider": provider,
        "name": getattr(prov, "label", provider),
        "has_cookie": bool(cookie),
        "logged_in": False,
        "nickname": None,
        "avatar": None,
        "uid": "",
        "vip": False,
        "level": "",
        "login_mode": "qr" if "qr" in getattr(prov, "login_modes", ("cookie",)) else "cookie",
        "playlists_supported": bool(getattr(prov, "playlists_supported", False)),
        "note": getattr(prov, "account_note", "") or None,
    }
    if info is not None:
        payload.update(
            {
                "logged_in": bool(info.logged_in),
                "nickname": info.nickname or None,
                "avatar": info.avatar or None,
                "uid": info.uid or "",
                "vip": bool(info.vip),
                "level": info.level or "",
            }
        )
    return payload


async def _probe(provider: str, cookie: str, fallback: str = "") -> dict[str, Any]:
    """用 cookie 问平台「我是谁」。平台抽风时降级成「有 Cookie 但身份未验证」。"""
    prov = state.registry.get(provider)
    if not cookie:
        return _account_payload(provider, None, "")
    try:
        info = await prov.account_info(cookie)
    except PROVIDER_ERRORS as exc:
        log.warning("账号校验失败 %s: %s", provider, exc)
        return _account_payload(provider, None, cookie)
    except Exception as exc:  # noqa: BLE001 - 面板不能因为一个平台挂掉就整页报错
        log.exception("账号校验异常 %s", provider)
        return _account_payload(provider, None, cookie)
    if info is None:
        # 平台不支持验证身份，但有凭据就认（fallback 是已存 Cookie 的场景）
        return _account_payload(provider, None, cookie or fallback)
    return _account_payload(provider, info, cookie)


def _remember(provider: str, cookie: str) -> None:
    """存凭据并让共享 client 立刻带上它；目录不可写时给可读的 400 而不是 500。"""
    try:
        state.accounts.set(provider, cookie)
    except StoreError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"{exc}。把 MDL_ACCOUNT_FILE 指到一个可写路径即可（凭据不会上传到别处）",
        ) from exc
    state.http.set_cookie(provider, cookie)


def _cookie_for(provider: str) -> str:
    """账号优先：面板里存过的 Cookie 覆盖配置/环境变量里的初始值。"""
    return state.accounts.get(provider) or settings.cookie_for(provider)


@app.get("/api/accounts")
async def list_accounts() -> dict[str, Any]:
    """四个平台各自的身份状态，前端一次拿全。"""
    accounts = []
    for name in state.registry.names():
        stored = state.accounts.get(name)
        card = await _probe(name, stored or settings.cookie_for(name))
        # has_cookie 问的是「用户存过凭据吗」，不是「请求带不带匿名 Cookie」
        # （网易云/酷狗的匿名请求本来就带 os=pc 这类默认 Cookie）
        card["has_cookie"] = bool(stored)
        accounts.append(card)
    return {"ok": True, "accounts": accounts}


@app.post("/api/accounts/{provider}")
async def save_account(provider: str, body: LoginBody) -> dict[str, Any]:
    """保存粘贴的 Cookie。必须真能问出身份才算成功，否则让用户知道它没用。"""
    prov = _provider_or_400(provider)
    cookie = (body.cookie or "").strip()
    if not cookie:
        raise HTTPException(status_code=400, detail="Cookie 不能为空")
    try:
        info = await prov.account_info(cookie)
    except AccountError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PROVIDER_ERRORS as exc:
        raise HTTPException(status_code=400, detail=f"Cookie 无效：{exc}") from exc
    if info is None or not info.logged_in:
        # 平台能存但验不出身份：照样存下去（可能只是接口抽风），但如实回报
        _remember(provider, cookie)
        return {"ok": True, "account": _account_payload(provider, info, cookie)}
    _remember(provider, cookie)
    return {"ok": True, "account": _account_payload(provider, info, cookie)}


@app.delete("/api/accounts/{provider}")
async def delete_account(provider: str) -> dict[str, Any]:
    _provider_or_400(provider)
    try:
        state.accounts.clear(provider)
    except StoreError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    state.http.set_cookie(provider, settings.cookie_for(provider))
    return {"ok": True}


@app.post("/api/accounts/{provider}/qr")
async def account_qr_start(provider: str) -> dict[str, Any]:
    prov = _provider_or_400(provider)
    modes = getattr(prov, "login_modes", ("cookie",))
    if "qr" not in modes:
        raise HTTPException(status_code=400, detail=f"{provider} 不支持扫码登录，请粘贴 Cookie")
    try:
        data = await prov.qr_start()
    except NotImplementedError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PROVIDER_ERRORS as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - 平台侧花样太多，别让前端只看到 500
        log.exception("申请二维码异常 %s", provider)
        raise HTTPException(status_code=502, detail=_exc_detail(exc)) from exc
    return {"ok": True, **data}


@app.get("/api/accounts/{provider}/qr/{key}")
async def account_qr_poll(provider: str, key: str) -> dict[str, Any]:
    prov = _provider_or_400(provider)
    if "qr" not in getattr(prov, "login_modes", ("cookie",)):
        raise HTTPException(status_code=400, detail=f"{provider} 不支持扫码登录")
    try:
        result = await prov.qr_poll(key)
    except NotImplementedError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except AccountError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PROVIDER_ERRORS as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - 状态查询必须总能给出可读原因
        log.exception("扫码状态查询异常 %s", provider)
        raise HTTPException(status_code=502, detail=_exc_detail(exc)) from exc

    status = str(result.get("status") or "wait")
    if status not in QR_STATUSES:
        status = "wait"
    payload: dict[str, Any] = {
        "ok": True,
        "status": status,
        "message": result.get("message") or "",
    }
    if status == "ok":
        cookie = (result.get("cookie") or "").strip()
        if cookie:
            try:
                state.accounts.set(provider, cookie)
            except StoreError as exc:
                # 落盘失败不该让「登录成功」当场变成失败：内存里已经存住了，
                # 这次会话照样能用，只是重启后要重扫
                log.warning("扫码登录凭据落盘失败，仅保留在内存：%s", exc)
            state.http.set_cookie(provider, cookie)
        account = result.get("account")
        if account:
            fields = {f.name for f in dataclasses.fields(AccountInfo)}
            payload["account"] = _account_payload(
                provider,
                AccountInfo(
                    **{k: v for k, v in account.items() if k in fields and k != "provider"},
                    provider=provider,
                ),
                cookie,
            )
        else:
            payload["account"] = await _probe(provider, cookie)
    return payload


@app.get("/api/accounts/{provider}/playlists")
async def account_playlists(provider: str) -> dict[str, Any]:
    prov = _provider_or_400(provider)
    cookie = _cookie_for(provider)
    note = getattr(prov, "account_note", "") or ""
    if not getattr(prov, "playlists_supported", False):
        return {"ok": True, "playlists": [], "note": note or "该平台暂不支持读取账号歌单"}
    try:
        playlists = await prov.user_playlists(cookie)
    except AccountError as exc:
        return {"ok": True, "playlists": [], "note": str(exc)}
    except PROVIDER_ERRORS as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "ok": True,
        "playlists": [
            {
                "id": p.id,
                "name": p.name,
                "track_count": p.track_count,
                "cover": p.cover_url,
                "creator": p.creator,
            }
            for p in playlists
        ],
        "note": note,
    }


@app.get("/api/playlists/{provider}/{playlist_id}")
async def playlist_tracks(provider: str, playlist_id: str) -> dict[str, Any]:
    prov = _provider_or_400(provider)
    try:
        title, tracks = await prov.playlist_tracks(playlist_id, _cookie_for(provider))
    except AccountError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PROVIDER_ERRORS as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "ok": True,
        "provider": provider,
        "id": playlist_id,
        "title": title,
        "total": len(tracks),
        "tracks": [t.to_dict() for t in tracks],
    }


# ------------------------------------------------------------------ 搜索


@app.get("/api/search")
async def search(
    q: str = Query(..., min_length=1, description="关键词"),
    provider: str = Query("all", description="all 或具体平台名"),
    limit: int = Query(20, ge=1, le=50),
) -> dict[str, Any]:
    targets = state.registry.names() if provider == "all" else [provider]
    for name in targets:
        if name not in state.registry.providers:
            raise HTTPException(status_code=400, detail=f"不支持的平台: {name}")

    groups: list[dict[str, Any]] = []
    for name in targets:
        prov = state.registry.get(name)
        try:
            tracks = await prov.search(q, limit)
            groups.append(
                {
                    "provider": name,
                    "ok": True,
                    "error": "",
                    "tracks": [t.to_dict() for t in tracks],
                }
            )
        except PROVIDER_ERRORS as exc:
            log.warning("搜索失败 %s: %s", name, exc)
            groups.append({"provider": name, "ok": False, "error": str(exc), "tracks": []})
        except Exception as exc:  # 单个平台炸掉不影响其他平台
            log.exception("搜索异常 %s", name)
            groups.append(
                {"provider": name, "ok": False, "error": f"内部错误: {exc}", "tracks": []}
            )
    return {"query": q, "groups": groups}


# ------------------------------------------------------------------ 解析


@app.post("/api/resolve")
async def resolve(body: ResolveBody) -> dict[str, Any]:
    try:
        target = state.registry.detect(body.url)
        provider = state.registry.get(target.provider)
        result = await provider.resolve_url(body.url)
    except PROVIDER_ERRORS as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        log.exception("解析失败")
        raise HTTPException(status_code=502, detail=f"解析失败: {exc}") from exc
    payload = result.to_dict()
    payload["target"] = target.to_dict()
    return payload


# ------------------------------------------------------------------ 下载


def _check_quality(quality: str) -> str:
    if quality not in QUALITIES:
        raise HTTPException(status_code=400, detail=f"音质必须是 {QUALITIES} 之一")
    return quality


async def _enrich(track: Track) -> Track:
    """前端只传了搜索结果的部分字段时，回查一次详情补齐时长/封面。

    播放和下载都靠它：酷我用时长估算是否试听片段，缺时长就判不出来。
    """
    if track.duration_ms and track.cover_url:
        return track
    try:
        full = await state.registry.get(track.provider).track_from_id(track.id)
    except Exception:  # noqa: BLE001 - 补全失败不该拦住主流程
        return track
    track.duration_ms = track.duration_ms or full.duration_ms
    track.cover_url = track.cover_url or full.cover_url
    track.album = track.album or full.album
    track.artists = track.artists or full.artists
    track.title = track.title or full.title
    track.extra = {**full.extra, **track.extra}
    return track


@app.post("/api/download")
async def download(body: DownloadBody) -> dict[str, Any]:
    _check_quality(body.quality)
    if body.provider not in state.registry.providers:
        raise HTTPException(status_code=400, detail=f"不支持的平台: {body.provider}")
    track = body.to_track()
    await _enrich(track)

    result = await state.downloader.download(track, body.quality)
    if not result.ok:
        raise HTTPException(status_code=502, detail=result.error)
    return result.to_dict()


@app.post("/api/download/batch")
async def download_batch(body: BatchBody) -> dict[str, Any]:
    _check_quality(body.quality)
    tracks: list[Track] = []
    for tb in body.tracks:
        if tb.provider not in state.registry.providers:
            raise HTTPException(status_code=400, detail=f"不支持的平台: {tb.provider}")
        tracks.append(tb.to_track())
    job = state.jobs.create(tracks, body.quality)
    return {"job_id": job.id, "total": job.total}


@app.get("/api/jobs/{job_id}")
async def job_detail(job_id: str) -> dict[str, Any]:
    job = state.jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    return job.to_dict()


@app.get("/api/jobs")
async def job_list() -> dict[str, Any]:
    jobs = sorted(state.jobs.jobs.values(), key=lambda j: j.created_at, reverse=True)
    return {"jobs": [j.to_dict() for j in jobs[:20]]}


# ------------------------------------------------------------------ 在线播放


class PlayBody(TrackBody):
    quality: str = "320"


@app.post("/api/play/prepare")
async def play_prepare(body: PlayBody) -> dict[str, Any]:
    """解析出可播放的直链，返回一个短令牌给 <audio> 用。"""
    _check_quality(body.quality)
    if body.provider not in state.registry.providers:
        raise HTTPException(status_code=400, detail=f"不支持的平台: {body.provider}")
    track = await _enrich(body.to_track())
    try:
        entry = await state.play.prepare(track, body.quality)
    except StreamUnavailable as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except PROVIDER_ERRORS as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        log.exception("播放解析失败")
        raise HTTPException(status_code=502, detail=f"播放解析失败: {exc}") from exc
    return entry.to_dict()


@app.get("/api/play/{token}")
async def play_stream(token: str, request: Request) -> Response:
    """把上游音频流代理给浏览器，支持 Range（拖动进度条）。"""
    entry = state.play.get(token)
    if entry is None:
        raise HTTPException(status_code=404, detail="播放令牌已过期，请重新点击播放")
    return await proxy(state.http, entry, request.headers.get("range", ""), request.method)


# ------------------------------------------------------------------ 本地文件


@app.get("/api/file")
async def local_file(path: str = Query(..., description="下载目录内的相对路径")) -> FileResponse:
    root = Path(settings.download_dir).resolve()
    try:
        target = (root / path).resolve()
    except (OSError, ValueError):
        raise HTTPException(status_code=400, detail="路径非法") from None
    if not str(target).startswith(str(root)):
        raise HTTPException(status_code=403, detail="只能访问下载目录内的文件")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(target, filename=target.name)


# ------------------------------------------------------------------ 静态页面


@app.exception_handler(StreamUnavailable)
async def _stream_unavailable(_request, exc: StreamUnavailable) -> JSONResponse:
    return JSONResponse(status_code=502, content={"detail": str(exc)})


if WEB_DIR.is_dir():
    # 前端资源全部用相对路径引用（./app.css、./app.js），所以同一份文件既能挂
    # 在本服务根路径，也能塞进任何子路径下托管。
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html", media_type="text/html")

    @app.get("/index.html")
    async def index_html() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html", media_type="text/html")

    @app.get("/app.css")
    async def app_css() -> FileResponse:
        return FileResponse(WEB_DIR / "app.css", media_type="text/css")

    @app.get("/app.js")
    async def app_js() -> FileResponse:
        return FileResponse(WEB_DIR / "app.js", media_type="text/javascript")

    @app.get("/account.js")
    async def account_js() -> FileResponse:
        return FileResponse(WEB_DIR / "account.js", media_type="text/javascript")

    @app.get("/account.css")
    async def account_css() -> FileResponse:
        return FileResponse(WEB_DIR / "account.css", media_type="text/css")

    @app.get("/player.js")
    async def player_js() -> FileResponse:
        return FileResponse(WEB_DIR / "player.js", media_type="text/javascript")

    @app.get("/player.css")
    async def player_css() -> FileResponse:
        return FileResponse(WEB_DIR / "player.css", media_type="text/css")

    @app.get("/favicon.ico")
    async def favicon() -> Response:
        # 内联一个音符图标，省得浏览器每开一次就 404 一条
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
            'stroke="#7c9cff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
            '<path d="M9 17.4V6.2l9-1.9v11.2"/><circle cx="6.6" cy="17.6" r="2.4"/>'
            '<circle cx="15.6" cy="15.5" r="2.4"/></svg>'
        )
        return Response(content=svg, media_type="image/svg+xml")


def run(host: str = "127.0.0.1", port: int = 8000, reload: bool = False) -> None:
    import uvicorn

    uvicorn.run("mdl.server:app" if reload else app, host=host, port=port, reload=reload)
