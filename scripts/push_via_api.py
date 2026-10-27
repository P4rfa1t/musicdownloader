"""直连 github.com 不通时，用 Git Data API 把本地提交推上去。

某些网络环境下 `github.com:443` 会超时（`git push` 直接失败），而
`api.github.com` 仍然可达。这个脚本就绕开 git 传输层，直接调
Git Data API 完成等价的推送：

    建 blob -> 建 tree（base_tree = 远端 main 的 tree）-> 建 commit -> 移动 ref

再补一个 annotated tag。适合应急，日常仍然建议用 `git push`。

用法：

    set GITHUB_TOKEN=ghp_xxx        # Windows
    export GITHUB_TOKEN=ghp_xxx     # bash
    python scripts/push_via_api.py [--tag v1.2.0]

令牌只从环境变量读，不落盘、不进仓库。需要 token 有 `repo` 权限。
"""

from __future__ import annotations

import argparse
import base64
import os
import subprocess
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
OWNER, REPO = "P4rfa1t", "musicdownloader"
API = f"https://api.github.com/repos/{OWNER}/{REPO}"


def git(*args: str) -> str:
    # 提交信息是 UTF-8，控制台默认 GBK 会解码崩，这里强制按 UTF-8 解
    return subprocess.run(
        ["git", *args],
        cwd=ROOT,
        capture_output=True,
        check=True,
        encoding="utf-8",
        errors="replace",
    ).stdout


def main() -> int:
    parser = argparse.ArgumentParser(description="用 Git Data API 推送当前分支")
    parser.add_argument("--branch", default="main", help="目标分支（默认 main）")
    parser.add_argument("--tag", default="", help="同时把该 tag 指到新提交")
    args = parser.parse_args()

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        print("缺少 GITHUB_TOKEN 环境变量", file=sys.stderr)
        return 2

    client = httpx.Client(
        timeout=120,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
    )

    remote_main = client.get(f"{API}/git/ref/heads/{args.branch}").json()["object"]["sha"]
    local_head = git("rev-parse", "HEAD").strip()
    print("remote:", remote_main[:8], "local:", local_head[:8])
    if remote_main == local_head:
        print("远端已是最新，退出")
        return 0

    full_message = git("log", "-1", "--format=%B", local_head).rstrip()

    # 远端那个提交不一定在本地仓库里（它是 API 造出来的），所以不能 git diff。
    # 改成两边各取一份 path->blob sha 的映射来比。
    base_tree = client.get(f"{API}/git/commits/{remote_main}").json()["tree"]["sha"]
    remote_tree = client.get(f"{API}/git/trees/{base_tree}", params={"recursive": "1"})
    remote_tree.raise_for_status()
    remote_blobs = {
        item["path"]: item["sha"] for item in remote_tree.json()["tree"] if item["type"] == "blob"
    }

    local_blobs = {}
    for entry in git("ls-tree", "-r", "-z", local_head).split("\0"):
        if not entry.strip():
            continue
        meta, path = entry.split("\t", 1)
        _mode, _type, sha = meta.split()
        local_blobs[path] = sha

    changed = []
    for path, sha in local_blobs.items():
        if remote_blobs.get(path) != sha:
            changed.append(path)
    deleted = [path for path in remote_blobs if path not in local_blobs]
    print(f"待同步 {len(changed)} 个文件，删除 {len(deleted)} 个")
    if not changed and not deleted:
        print("没有文件差异，无需推送")
        return 0

    tree_items = []
    for path in deleted:
        # sha=null 表示从 tree 里删掉这个路径
        tree_items.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
    for path in changed:
        # 从 git 对象库取内容，而不是读工作区：autocrlf 下工作区是 CRLF，
        # 读工作区会把 CRLF 写进仓库，产生无意义的整文件差异
        raw = subprocess.run(
            ["git", "cat-file", "blob", local_blobs[path]],
            cwd=ROOT,
            capture_output=True,
            check=True,
        ).stdout
        blob = client.post(
            f"{API}/git/blobs",
            json={"content": base64.b64encode(raw).decode(), "encoding": "base64"},
        )
        blob.raise_for_status()
        tree_items.append({"path": path, "mode": "100644", "type": "blob", "sha": blob.json()["sha"]})
        print("  blob", path, len(raw), "B")

    tree = client.post(f"{API}/git/trees", json={"base_tree": base_tree, "tree": tree_items})
    tree.raise_for_status()

    commit = client.post(
        f"{API}/git/commits",
        json={"message": full_message, "tree": tree.json()["sha"], "parents": [remote_main]},
    )
    commit.raise_for_status()
    new_sha = commit.json()["sha"]
    print("新提交:", new_sha[:8])

    upd = client.patch(f"{API}/git/refs/heads/{args.branch}", json={"sha": new_sha, "force": False})
    upd.raise_for_status()
    print(f"{args.branch} ->", upd.json()["object"]["sha"][:8])

    if args.tag:
        existing = client.get(f"{API}/git/ref/tags/{args.tag}")
        if existing.status_code == 200:
            client.patch(f"{API}/git/refs/tags/{args.tag}", json={"sha": new_sha, "force": True})
            print("tag 已移动:", args.tag)
        else:
            tag_obj = client.post(
                f"{API}/git/tags",
                json={
                    "tag": args.tag,
                    "message": f"MusicDL {args.tag}",
                    "object": new_sha,
                    "type": "commit",
                },
            )
            tag_obj.raise_for_status()
            client.post(f"{API}/git/refs", json={"ref": f"refs/tags/{args.tag}", "sha": tag_obj.json()["sha"]})
            print("tag 已创建:", args.tag)
    return 0


if __name__ == "__main__":
    sys.exit(main())