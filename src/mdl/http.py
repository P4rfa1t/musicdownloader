"""共享 HTTP 客户端：统一 UA / Referer / Cookie / 重试 / 超时。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from .config import settings

log = logging.getLogger("mdl.http")


class HttpError(RuntimeError):
    """网络层失败，已耗尽重试。"""


class Http:
    """轻量封装，一个实例复用一个连接池。

    用法::

        async with Http() as http:
            data = await http.get_json("https://...", referer="https://music.163.com/")
    """

    def __init__(self, cookies: dict[str, str] | None = None) -> None:
        self._cookies = cookies or {}
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "Http":
        await self.open()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def open(self) -> None:
        if self._client is not None:
            return
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.timeout, connect=10.0),
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
            headers={"User-Agent": settings.user_agent},
            follow_redirects=True,
            proxy=settings.proxy or None,
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise HttpError("Http 未初始化，请先 await http.open()")
        return self._client

    def set_cookie(self, provider: str, cookie: str) -> None:
        """运行期换 Cookie（登录 / 退出面板操作后立刻生效，不用重启服务）。"""
        cookie = (cookie or "").strip()
        if cookie:
            self._cookies[provider] = cookie
        else:
            self._cookies.pop(provider, None)

    def headers_for(self, provider: str, referer: str = "") -> dict[str, str]:
        h: dict[str, str] = {"User-Agent": settings.user_agent}
        if referer:
            h["Referer"] = referer
        cookie = self._cookies.get(provider, "")
        if cookie:
            h["Cookie"] = cookie
        return h

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        data: Any = None,
        retries: int | None = None,
    ) -> httpx.Response:
        attempts = settings.retries if retries is None else max(1, retries)
        last: Exception | None = None
        for i in range(attempts):
            try:
                resp = await self.client.request(
                    method,
                    url,
                    headers=headers,
                    params=params,
                    json=json_body,
                    data=data,
                )
                if resp.status_code >= 500:
                    raise HttpError(f"HTTP {resp.status_code} {url}")
                return resp
            except (httpx.HTTPError, HttpError) as exc:  # 网络抖动统一重试
                last = exc
                if i < attempts - 1:
                    await asyncio.sleep(0.4 * (2**i))
                    log.debug("重试 %s %s (%s)", method, url, exc)
        raise HttpError(f"{method} {url} 失败: {last}")

    async def get_json(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        resp = await self.request("GET", url, headers=headers, params=params)
        try:
            return resp.json()
        except ValueError as exc:
            raise HttpError(f"响应不是 JSON: {url}") from exc

    async def post_json(
        self,
        url: str,
        body: Any,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        resp = await self.request("POST", url, headers=headers, json_body=body, params=params)
        try:
            return resp.json()
        except ValueError as exc:
            raise HttpError(f"响应不是 JSON: {url}") from exc

    async def get_text(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> str:
        resp = await self.request("GET", url, headers=headers, params=params)
        return resp.text
