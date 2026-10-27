"""端到端验证播放代理：真起服务 → 真搜索 → prepare → 拉前 256 KB。"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
PY = ROOT / ".venv" / "Scripts" / "python.exe"
PORT = 8831
BASE = f"http://127.0.0.1:{PORT}"


async def main() -> int:
    async with httpx.AsyncClient(timeout=30) as c:
        for _ in range(60):
            try:
                if (await c.get(f"{BASE}/api/health")).status_code == 200:
                    break
            except Exception:
                pass
            await asyncio.sleep(0.5)
        else:
            print("服务没起来")
            return 1

        r = await c.get(f"{BASE}/api/search", params={"q": "屋顶", "provider": "netease"})
        tracks = [t for g in r.json()["groups"] for t in g["tracks"]]
        print("搜索到", len(tracks), "首；首条 =", tracks[0]["title"], tracks[0]["artists"])
        track = tracks[0]

        body = {
            "provider": track["provider"],
            "id": track["id"],
            "title": track["title"],
            "artists": track["artists"],
            "album": track.get("album"),
            "duration_ms": track.get("duration_ms"),
            "cover_url": track.get("cover_url"),
            "quality": "320",
        }
        r = await c.post(f"{BASE}/api/play/prepare", json=body)
        print("prepare:", r.status_code, r.json())
        if r.status_code != 200:
            return 1
        prep = r.json()

        # 整段 HEAD 式的头信息 + 前 256 KB
        r = await c.get(f"{BASE}/api/play/{prep['token']}", headers={"Range": "bytes=0-262143"})
        print(
            "range:",
            r.status_code,
            "len =", len(r.content),
            "ctype =", r.headers.get("content-type"),
            "crange =", r.headers.get("content-range"),
        )
        head = r.content[:3]
        print("前 3 字节 =", head, "(ID3 头应为 b'ID3')" if head == b"ID3" else "")

        # 播放到中段（模拟拖动进度条）
        mid = max(0, (prep.get("size") or 0) // 2)
        r2 = await c.get(
            f"{BASE}/api/play/{prep['token']}",
            headers={"Range": f"bytes={mid}-{mid + 65535}"},
        )
        print("seek:", r2.status_code, "len =", len(r2.content), "crange =", r2.headers.get("content-range"))

        r3 = await c.get(f"{BASE}/api/play/不存在的令牌")
        print("坏令牌:", r3.status_code, r3.json())
    return 0


if __name__ == "__main__":
    if "--attach" in sys.argv:
        sys.exit(asyncio.run(main()))
    srv = subprocess.Popen(
        [str(PY), "-m", "mdl", "serve", "--port", str(PORT), "--host", "127.0.0.1"],
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env={"PYTHONPATH": str(ROOT / "src"), "PATH": str(Path(PY).parent), "PYTHONIOENCODING": "utf-8"},
    )
    try:
        code = asyncio.run(main())
    finally:
        srv.terminate()
    sys.exit(code)