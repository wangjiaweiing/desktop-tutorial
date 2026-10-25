#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把最新的数据文件和页面发布到 GitHub 仓库。

默认走 GitHub 的 Git Data API（api.github.com），一次调用生成一个干净提交，
不依赖 github.com 的 git 协议——在 git push 被网络挡住的环境里也能用。

Token 来源优先级：环境变量 GH_TOKEN / GITHUB_TOKEN > 项目根目录 .env 里的 GH_TOKEN。
Token 不会写进任何文件或日志。

用法
    python scripts/publish.py                 # 有变化才提交
    python scripts/publish.py -m "自定义提交信息"
    python scripts/publish.py --dry-run       # 只看有哪些文件会变
    python scripts/publish.py --via git       # 改用 git push（需要能连通 github.com）
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REPO = "wangjiaweiing/desktop-tutorial"
BRANCH = "main"
API = "https://api.github.com"

# 允许发布的文件（相对路径）；不在列表里的一律不上传
PUBLISH_ALLOW = {
    ".gitattributes", ".gitignore", "README.md", "index.html",
    "scripts/collect.py", "scripts/publish.py",
    "data/repos.json", "data/repos.js", "data/seen_repos.json", "data/manual_notes.json",
}


def load_dotenv(path: Path = ROOT / ".env") -> None:
    if not path.exists():
        return
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip().removeprefix("export ").strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val
    except OSError:
        pass


def run(args: list) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, cwd=str(ROOT), check=False,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def api(token: str, path: str, method: str = "GET", body=None):
    """返回 (status, data)；4xx/5xx 不抛异常。"""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(f"{API}{path}", data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "gh-trend-radar")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            raw = resp.read().decode("utf-8") or "{}"
            return resp.status, json.loads(raw)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace") or "{}"
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"message": raw[:300]}
    except (urllib.error.URLError, OSError) as exc:
        return 0, {"message": f"网络错误：{exc}"}


# --------------------------------------------------------------------------
# 文件收集与内容比对
# --------------------------------------------------------------------------

def publish_files() -> list:
    """返回要发布的文件相对路径列表（按 PUBLISH_ALLOW 过滤）。"""
    out = []
    for rel in sorted(PUBLISH_ALLOW):
        p = ROOT / rel
        if p.is_file():
            out.append(rel)
    return out


def blob_sha(path: Path) -> str:
    """本地计算 git blob sha，用来和远程树里的 sha 比对，避免重复上传。"""
    proc = run(["git", "hash-object", "--", str(path)])
    if proc.returncode == 0:
        return proc.stdout.strip()
    # 没装 git 时的兜底：sha1("blob <len>\\0" + content)
    import hashlib
    data = path.read_bytes()
    h = hashlib.sha1()
    h.update(f"blob {len(data)}\0".encode())
    h.update(data)
    return h.hexdigest()


def remote_tree(token: str, repo: str, branch: str):
    """返回 (head_sha, {path: blob_sha})；仓库为空时返回 (None, {})。"""
    status, ref = api(token, f"/repos/{repo}/git/ref/heads/{branch}")
    if status != 200:
        return None, {}
    head = ref["object"]["sha"]
    status, commit = api(token, f"/repos/{repo}/git/commits/{head}")
    if status != 200:
        return head, {}
    status, tree = api(token, f"/repos/{repo}/git/trees/{commit['tree']['sha']}?recursive=1")
    if status != 200:
        return head, {}
    files = {n["path"]: n["sha"] for n in tree.get("tree", []) if n.get("type") == "blob"}
    return head, files


# --------------------------------------------------------------------------
# 发布方式一：Git Data API（默认）
# --------------------------------------------------------------------------

def publish_via_api(token: str, repo: str, branch: str, msg: str, dry: bool) -> int:
    files = publish_files()
    if not files:
        print("❌ 没有找到可发布的文件")
        return 1

    head, remote = remote_tree(token, repo, branch)
    local = {rel: blob_sha(ROOT / rel) for rel in files}

    changed = [rel for rel in files if remote.get(rel) != local[rel]]
    deleted = [rel for rel in remote if rel not in local and rel.startswith(("data/", "scripts/"))]

    if not changed and not deleted:
        print("· 数据无变化，跳过提交")
        return 0

    print(f"· 远程 HEAD：{head[:7] if head else '（空仓库）'}")
    print(f"· 本次变更 {len(changed)} 个文件：")
    for rel in changed:
        print(f"    {'新增' if rel not in remote else '修改'}  {rel}")

    if dry:
        print("· dry-run，未提交")
        return 0

    # 0. 空仓库没法直接建 blob，先用 Contents API 落下第一个文件
    if head is None and changed:
        first = changed[0]
        content = base64.b64encode((ROOT / first).read_bytes()).decode("ascii")
        status, res = api(token, f"/repos/{repo}/contents/{first}", "PUT",
                          {"message": msg, "content": content, "branch": branch})
        if status not in (200, 201):
            print(f"❌ 初始化仓库失败：{res.get('message')}")
            return 1
        print(f"· 空仓库已初始化（{first}）")
        head, remote = remote_tree(token, repo, branch)
        changed = [rel for rel in files if remote.get(rel) != local[rel]]
        if not changed:
            print(f"✅ 已发布 {repo}@{branch} → {head[:7]}")
            return 0

    # 1. 上传变更的 blob
    entries = []
    for rel in changed:
        content = base64.b64encode((ROOT / rel).read_bytes()).decode("ascii")
        status, blob = api(token, f"/repos/{repo}/git/blobs", "POST",
                           {"content": content, "encoding": "base64"})
        if status not in (200, 201):
            print(f"❌ 上传 {rel} 失败：{blob.get('message')}")
            return 1
        entries.append({"path": rel, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        print(f"    ↑ {rel}")

    for rel in deleted:
        entries.append({"path": rel, "mode": "100644", "type": "blob", "sha": None})
        print(f"    ✕ 删除 {rel}")

    # 2. 建树
    tree_body = {"tree": entries}
    if head:
        status, base = api(token, f"/repos/{repo}/git/commits/{head}")
        if status == 200:
            tree_body["base_tree"] = base["tree"]["sha"]
    status, tree = api(token, f"/repos/{repo}/git/trees", "POST", tree_body)
    if status not in (200, 201):
        print(f"❌ 建树失败：{tree.get('message')}")
        return 1

    # 3. 提交
    commit_body = {"message": msg, "tree": tree["sha"], "parents": [head] if head else []}
    status, commit = api(token, f"/repos/{repo}/git/commits", "POST", commit_body)
    if status not in (200, 201):
        print(f"❌ 创建提交失败：{commit.get('message')}")
        return 1

    # 4. 移动分支
    if head:
        status, res = api(token, f"/repos/{repo}/git/refs/heads/{branch}", "PATCH",
                          {"sha": commit["sha"], "force": False})
    else:
        status, res = api(token, f"/repos/{repo}/git/refs", "POST",
                          {"ref": f"refs/heads/{branch}", "sha": commit["sha"]})
    if status not in (200, 201):
        print(f"❌ 更新分支失败：{res.get('message')}")
        return 1

    print(f"✅ 已发布 {repo}@{branch} → {commit['sha'][:7]}")
    return 0


# --------------------------------------------------------------------------
# 发布方式二：git push（需要能连通 github.com）
# --------------------------------------------------------------------------

def publish_via_git(token: str, repo: str, branch: str, msg: str, dry: bool) -> int:
    if not (ROOT / ".git").exists():
        run(["git", "init", "-b", branch])
    if not run(["git", "config", "user.email"]).stdout.strip():
        run(["git", "config", "user.email", "bot@users.noreply.github.com"])
    if not run(["git", "config", "user.name"]).stdout.strip():
        run(["git", "config", "user.name", "trend-bot"])
    if not run(["git", "remote", "get-url", "origin"]).stdout.strip():
        run(["git", "remote", "add", "origin", f"https://github.com/{repo}.git"])

    run(["git", "add", "-A"])
    staged = run(["git", "diff", "--cached", "--quiet"]).returncode != 0
    if not staged:
        print("· 数据无变化，跳过提交")
        return 0
    if dry:
        print("· dry-run，未提交")
        return 0

    run(["git", "commit", "-m", msg])
    url = f"https://x-access-token:{token}@github.com/{repo}.git"
    proc = run(["git", "push", url, f"HEAD:{branch}"])
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").replace(token, "***").strip()
        print(f"❌ 推送失败：{err[:300]}")
        return 1
    print(f"✅ 已推送 {repo}@{branch}")
    return 0


def main() -> int:
    load_dotenv()
    token = (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or "").strip()
    repo = (os.environ.get("GH_REPO") or DEFAULT_REPO).strip()
    branch = (os.environ.get("GH_BRANCH") or BRANCH).strip()
    dry = "--dry-run" in sys.argv
    via = "api"
    if "--via" in sys.argv:
        i = sys.argv.index("--via")
        if i + 1 < len(sys.argv):
            via = sys.argv[i + 1].strip().lower()

    msg = None
    if "-m" in sys.argv:
        i = sys.argv.index("-m")
        if i + 1 < len(sys.argv):
            msg = sys.argv[i + 1]
    msg = msg or f"chore: 更新趋势数据 {datetime.now().strftime('%Y-%m-%d')}"

    if not token:
        print("❌ 缺少 GH_TOKEN，无法发布。请写入项目根目录 .env 或设置环境变量。")
        return 1

    if via == "git":
        return publish_via_git(token, repo, branch, msg, dry)
    return publish_via_api(token, repo, branch, msg, dry)


if __name__ == "__main__":
    sys.exit(main())
