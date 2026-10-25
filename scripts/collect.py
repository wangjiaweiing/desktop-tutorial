#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GitHub 趋势雷达 · 数据采集器

纯标准库实现，无任何第三方依赖。

环境变量（也可写在项目根目录 .env 里，键值一行一个）
    GH_TOKEN      GitHub Token（强烈建议，否则限额只有 60 次/小时）
    LLM_API_KEY   可选，配了才走外部大模型；不配则由智能体本人写说明
    LLM_BASE_URL  默认 https://api.deepseek.com/v1
    LLM_MODEL     默认 deepseek-chat

可选调参
    POOL_PAGES  候选池页数，默认 5（5 x 100 = 500）
    LIST_N      写入列表的仓库数，默认 120
    DELTA_N     实际算增量的数量，默认 0 表示全部
    NEW_N       额外纳入的新项目名额，默认 30
    CONCURRENCY 并发数，默认 4

用法（推荐两阶段，由智能体在中间补说明）
    python scripts/collect.py --stage fetch   # 采数据 + 抓新仓库 README → data/_pending.json
    # 智能体读 _pending.json，把说明写进 data/manual_notes.json
    python scripts/collect.py --stage apply   # 合并说明 → data/repos.json + repos.js
    python scripts/collect.py                 # 等价于 fetch + apply 连跑（不调模型）
    python scripts/collect.py --with-llm      # 额外用 LLM_API_KEY 自动生成说明
"""

from __future__ import annotations

import base64
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
REPOS_JSON = DATA_DIR / "repos.json"
REPOS_JS = DATA_DIR / "repos.js"
SEEN_JSON = DATA_DIR / "seen_repos.json"
CACHE_JSON = DATA_DIR / "_cache.json"      # fetch 与 apply 之间的中转（不进版本库）
PENDING_JSON = DATA_DIR / "_pending.json"  # 待智能体补写说明的仓库清单


def load_dotenv(path: Path = ROOT / ".env") -> None:
    """极简 .env 读取：KEY=VALUE，忽略注释与空行，不覆盖已有环境变量。"""
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


load_dotenv()

API = "https://api.github.com"
JSON_ACCEPT = "application/vnd.github+json"

GH_TOKEN = (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or "").strip()
LLM_API_KEY = (os.environ.get("LLM_API_KEY") or "").strip()
LLM_BASE_URL = (os.environ.get("LLM_BASE_URL") or "https://api.deepseek.com/v1").rstrip("/")
LLM_MODEL = os.environ.get("LLM_MODEL") or "deepseek-chat"

POOL_PAGES = int(os.environ.get("POOL_PAGES") or 5)
LIST_N = int(os.environ.get("LIST_N") or 120)          # 写入项目列表的仓库数
DELTA_N = int(os.environ.get("DELTA_N") or 0)          # 0 表示对全部候选算增量
NEW_N = int(os.environ.get("NEW_N") or 30)
STAR_FLOOR = int(os.environ.get("STAR_FLOOR") or 50)
ACTIVE_DAYS = int(os.environ.get("ACTIVE_DAYS") or 30)
NEW_WINDOW_DAYS = int(os.environ.get("NEW_WINDOW_DAYS") or 30)
CONCURRENCY = int(os.environ.get("CONCURRENCY") or 4)

MANUAL_NOTES = DATA_DIR / "manual_notes.json"

README_CHARS = 800
NO_LLM = "--no-llm" in sys.argv
WITH_LLM = "--with-llm" in sys.argv


def _arg_value(flag: str, default: str = "") -> str:
    if flag in sys.argv:
        idx = sys.argv.index(flag)
        if idx + 1 < len(sys.argv):
            return sys.argv[idx + 1]
    return default

# 领域标签：固定枚举，模型输出不在此列表内的一律改判「其他」
CATEGORIES = [
    "AI/机器学习", "前端开发", "后端/服务端", "数据库", "DevOps/运维",
    "命令行工具", "移动开发", "桌面应用", "浏览器扩展", "安全",
    "数据分析", "爬虫/自动化", "文档/笔记", "设计/UI", "区块链",
    "游戏开发", "学习资源", "其他",
]
CATEGORY_SET = set(CATEGORIES)
CATEGORY_ALIAS = {c.replace(" ", "").lower(): c for c in CATEGORIES}

# License 商用标注规则
PERMISSIVE = {"mit", "apache-2.0", "bsd-2-clause", "bsd-3-clause"}
COPYLEFT = {"gpl-2.0", "gpl-3.0", "agpl-3.0"}
LABEL_ALLOW = "✅ 允许商业二次开发"
LABEL_COPYLEFT = "⚠️ 可商用但衍生代码需开源"
LABEL_NONE = "❌ 未声明许可证，默认不可商用"
LABEL_UNKNOWN = "❓ 需人工确认"


# --------------------------------------------------------------------------
# 基础设施
# --------------------------------------------------------------------------

def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        log(f"⚠️  读取 {path.name} 失败（{exc}），按空数据处理")
        return default


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
        f.write("\n")
    tmp.replace(path)


_rate_lock = threading.Lock()
_last_call = [0.0]


def _throttle(gap: float = 0.15) -> None:
    """全局请求节流，避免瞬时并发触发二级限流。"""
    with _rate_lock:
        wait = _last_call[0] + gap - time.time()
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.time()


def api_get(url: str, accept: str = JSON_ACCEPT, retries: int = 4):
    """带限流退避的 GET，404 返回 None，其余错误抛异常。"""
    for attempt in range(retries):
        req = urllib.request.Request(url)
        req.add_header("Accept", accept)
        req.add_header("User-Agent", "gh-trend-radar")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        if GH_TOKEN:
            req.add_header("Authorization", f"Bearer {GH_TOKEN}")
        try:
            with urllib.request.urlopen(req, timeout=40) as resp:
                body = resp.read().decode("utf-8", "replace")
                return json.loads(body) if body.strip() else None
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            if exc.code in (403, 429):
                reset = exc.headers.get("X-RateLimit-Reset")
                retry_after = exc.headers.get("Retry-After")
                wait = 0
                if reset and reset.isdigit():
                    wait = int(reset) - int(time.time()) + 2
                elif retry_after and retry_after.isdigit():
                    wait = int(retry_after)
                wait = max(wait, 5)
                wait = min(wait, 120)
                if attempt == retries - 1:
                    raise RuntimeError(f"限流未恢复：{url}") from exc
                log(f"⏳ 触发限流({exc.code})，等待 {wait}s 后重试")
                time.sleep(wait)
                continue
            if 500 <= exc.code < 600:
                time.sleep(2 * (attempt + 1))
                continue
            detail = exc.read().decode("utf-8", "replace")[:200]
            raise RuntimeError(f"HTTP {exc.code} {url} :: {detail}") from exc
        except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
            if attempt == retries - 1:
                raise RuntimeError(f"网络失败：{url} :: {exc}") from exc
            time.sleep(2 * (attempt + 1))
    return None


# --------------------------------------------------------------------------
# GitHub 数据获取
# --------------------------------------------------------------------------

def search_repos(query: str, pages: int, per_page: int = 100) -> list:
    """GitHub 搜索 API 翻页，按 star 降序。"""
    out: list = []
    for page in range(1, pages + 1):
        params = urllib.parse.urlencode(
            {"q": query, "sort": "stars", "order": "desc",
             "per_page": per_page, "page": page}
        )
        _throttle(2.0)  # 搜索接口 30 次/分钟
        data = api_get(f"{API}/search/repositories?{params}")
        if not data:
            break
        items = data.get("items") or []
        out.extend(items)
        if len(items) < per_page:
            break
    return out


def fetch_star_delta(full_name: str, anchor):
    """返回 (仓库全名, 近 7 天 star 增量, 数据截止日)。

    接口按自然周分组返回 [{week, total, days[7]}]，最新一周在前。
    取最近 3 周拼成日序列，统计 anchor 往前 7 天的增量。
    anchor 由外部统一指定（昨日 UTC），保证所有仓库口径一致、可横向比较。
    """
    _throttle()
    anchor_date = datetime.strptime(anchor, "%Y-%m-%d").date()
    url = f"{API}/repos/{full_name}/stargazers/history?per_page=3"
    data = api_get(url)
    if not data:
        return full_name, None, None

    daily: dict = {}
    for week in data:
        start = week.get("week")
        days = week.get("days") or []
        if start is None:
            continue
        base = datetime.fromtimestamp(int(start), tz=timezone.utc).date()
        for idx, count in enumerate(days[:7]):
            day = base + timedelta(days=idx)
            daily[day] = daily.get(day, 0) + int(count or 0)

    if not daily:
        return full_name, None, None

    # 接口只给到上一个完整周（周六），周一到周六运行时「昨日」可能还没有数据。
    # 因此把窗口右端对齐到实际可用的最后一天，保证统计的始终是完整 7 天。
    end = min(anchor_date, max(daily))
    window = [d for d in daily if 0 <= (end - d).days < 7]
    return full_name, sum(daily[d] for d in window), end.isoformat()


BADGE_LINE = re.compile(r"(img\.shields\.io|badge\.svg|badgen\.net|travis-ci|codecov\.io)", re.I)


def clean_readme(raw: str, limit: int = README_CHARS) -> str:
    """把 README 压成纯文本并截断，控制 token 消耗。"""
    text = raw.replace("\r\n", "\n")
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)          # 图片
    text = re.sub(r"<[^>]{1,200}>", "", text)                  # HTML 标签
    text = re.sub(r"\[([^\]]{0,80})\]\([^)]*\)", r"\1", text)  # 链接保留文字
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.M)  # 标题符号
    text = re.sub(r"^\s*[|>].*$", "", text, flags=re.M)        # 表格/引用
    lines = [ln.strip() for ln in text.split("\n")]
    lines = [ln for ln in lines if ln and not BADGE_LINE.search(ln)]
    text = "\n".join(lines)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()[:limit]


def fetch_readme(full_name: str) -> str:
    _throttle()
    data = api_get(f"{API}/repos/{full_name}/readme")
    if not data:
        return ""
    content = data.get("content") or ""
    if not content:
        return ""
    try:
        raw = base64.b64decode(content).decode("utf-8", "ignore")
    except (ValueError, TypeError):
        return ""
    return clean_readme(raw)


def license_label(key) -> str:
    """按 license.key 打商用标注。"""
    k = (key or "").strip().lower()
    if k in PERMISSIVE:
        return LABEL_ALLOW
    if k in COPYLEFT:
        return LABEL_COPYLEFT
    if k in ("", "null", "none", "noassertion", "no-license"):
        return LABEL_NONE
    return LABEL_UNKNOWN


# --------------------------------------------------------------------------
# 大模型：领域标签 + 功能说明 + 使用方式，一次调用生成
# --------------------------------------------------------------------------

SYSTEM_PROMPT = """你是资深开源项目分析助手。根据仓库信息，输出严格 JSON：
{"category": "...", "summary": "...", "usage": "..."}

category 必须从以下固定列表中原样选择一个，不得自创、不得翻译、不得加后缀或解释：
AI/机器学习、前端开发、后端/服务端、数据库、DevOps/运维、命令行工具、移动开发、桌面应用、浏览器扩展、安全、数据分析、爬虫/自动化、文档/笔记、设计/UI、区块链、游戏开发、学习资源、其他
无法明确归类的，选「其他」。

summary：1~2 句中文，说清它是干什么用的、解决什么问题，不超过 60 字。不要重复仓库名，不要营销话术，不要用「这是一个」开头。
usage：一句中文，说清怎么上手，不超过 25 字。例如「命令行安装后直接运行」「克隆后浏览器打开 index.html」「Docker 一行命令拉起」。

只输出 JSON 对象本身，不要解释文字，不要代码块围栏。"""


def _extract_json(text: str):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None


def _normalize_category(value) -> str:
    if not isinstance(value, str):
        return "其他"
    raw = value.strip().strip("「」\"'")
    if raw in CATEGORY_SET:
        return raw
    return CATEGORY_ALIAS.get(raw.replace(" ", "").lower(), "其他")


def _llm_request(messages: list, json_mode: bool = True) -> str:
    payload = {
        "model": LLM_MODEL,
        "messages": messages,
        "temperature": 0,
        "max_tokens": 300,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(f"{LLM_BASE_URL}/chat/completions", data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {LLM_API_KEY}")
    with urllib.request.urlopen(req, timeout=90) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"]


def classify_repo(repo: dict) -> dict | None:
    """单仓库单次调用，返回 {category, summary, usage}；失败返回 None。"""
    full_name = repo["full_name"]
    readme = fetch_readme(full_name)
    if not readme:
        readme = (repo.get("description") or "")[:README_CHARS]

    user_content = (
        f"仓库：{full_name}\n"
        f"官方描述：{repo.get('description') or '（无）'}\n"
        f"主要语言：{repo.get('language') or '（未知）'}\n"
        f"星标数：{repo.get('stargazers_count', 0)}\n"
        f"README 前 800 字：\n{readme}"
    )
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]

    for attempt in range(2):
        try:
            try:
                raw = _llm_request(messages, json_mode=True)
            except urllib.error.HTTPError as exc:
                # 部分服务商不支持 response_format，降级重试
                if exc.code in (400, 422) and attempt == 0:
                    raw = _llm_request(messages, json_mode=False)
                else:
                    raise
            parsed = _extract_json(raw)
            if not parsed:
                raise ValueError("模型输出不是合法 JSON")
            summary = str(parsed.get("summary") or "").strip()
            usage = str(parsed.get("usage") or "").strip()
            if not summary:
                raise ValueError("summary 为空")
            return {
                "category": _normalize_category(parsed.get("category")),
                "summary": summary[:120],
                "usage": (usage or "见 README 快速开始")[:60],
            }
        except Exception as exc:  # noqa: BLE001 - 任何失败都走重试/跳过
            log(f"   ↳ 模型调用失败（{full_name}，第 {attempt + 1} 次）：{exc}")
            time.sleep(1.5)
    return None


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def build_record(repo: dict, delta, asof: str | None, previous: dict | None,
                 generated: dict | None, manual: dict | None, today: str) -> dict:
    """合并搜索结果、增量数据与说明文本，产出一条仓库记录。

    说明文本优先级：人工精校 > 模型生成 > 历史记录 > 官方描述兜底。
    """
    license_obj = repo.get("license") or {}
    lic_key = license_obj.get("key") or ""

    record = {
        "full_name": repo["full_name"],
        "url": repo.get("html_url") or f"https://github.com/{repo['full_name']}",
        "description": (repo.get("description") or "").strip(),
        "stars": int(repo.get("stargazers_count") or 0),
        "stars_7d": delta,
        "stars_7d_asof": asof,
        "language": repo.get("language") or "",
        "topics": (repo.get("topics") or [])[:6],
        "license_key": lic_key,
        "license_label": license_label(lic_key),
        "first_seen": (previous or {}).get("first_seen") or today,
        "last_seen": today,
    }

    if manual:
        record["category"] = _normalize_category(manual.get("category"))
        record["summary"] = manual["summary"]
        record["usage"] = manual["usage"]
        record["generated_by"] = manual.get("by") or "manual"
    elif generated:
        record["category"] = generated["category"]
        record["summary"] = generated["summary"]
        record["usage"] = generated["usage"]
        record["generated_by"] = "llm"
    else:
        record["category"] = (previous or {}).get("category") or "其他"
        record["summary"] = (previous or {}).get("summary") or record["description"] or "暂无说明"
        record["usage"] = (previous or {}).get("usage") or "见 README 快速开始"
        record["generated_by"] = (previous or {}).get("generated_by") or "fallback"

    return record




# --------------------------------------------------------------------------
# 阶段一：采集（搜索候选池 + 算增量 + 抓新仓库 README）
# --------------------------------------------------------------------------

def _slim(repo: dict) -> dict:
    """只保留后续要用的字段，控制中转文件体积。"""
    lic = repo.get("license") or {}
    return {
        "full_name": repo["full_name"],
        "html_url": repo.get("html_url") or f"https://github.com/{repo['full_name']}",
        "description": (repo.get("description") or "").strip(),
        "stargazers_count": int(repo.get("stargazers_count") or 0),
        "language": repo.get("language") or "",
        "topics": (repo.get("topics") or [])[:6],
        "license": {"key": lic.get("key") or ""},
        "pushed_at": repo.get("pushed_at") or "",
        "created_at": repo.get("created_at") or "",
    }


def pick_candidates() -> tuple:
    """返回 (按总 star 的候选, 近 30 天新建的高星候选)。"""
    today = datetime.now(timezone.utc).date()
    active_since = (today - timedelta(days=ACTIVE_DAYS)).isoformat()
    created_since = (today - timedelta(days=NEW_WINDOW_DAYS)).isoformat()

    log(f"拉取候选池：stars:>{STAR_FLOOR} pushed:>{active_since}（{POOL_PAGES} 页）")
    hot = search_repos(f"stars:>{STAR_FLOOR} pushed:>{active_since}", POOL_PAGES)
    hot.sort(key=lambda r: r.get("stargazers_count") or 0, reverse=True)
    log(f"  候选池共 {len(hot)} 个")

    picked: set = set()
    hot_sel: list = []
    for repo in hot[:LIST_N]:
        name = repo["full_name"]
        if name in picked:
            continue
        picked.add(name)
        hot_sel.append(repo)

    fresh_sel: list = []
    if NEW_N > 0:
        log(f"补充新项目名额：created:>{created_since}（取 {NEW_N} 个）")
        fresh = search_repos(f"stars:>{STAR_FLOOR} created:>{created_since}", 1,
                             per_page=max(NEW_N, 30))
        for repo in fresh:
            if len(fresh_sel) >= NEW_N:
                break
            name = repo["full_name"]
            if name in picked:
                continue
            picked.add(name)
            fresh_sel.append(repo)
        log(f"  实际补充 {len(fresh_sel)} 个新项目")

    return hot_sel, fresh_sel


def stage_fetch() -> int:
    started = time.time()
    today = datetime.now(timezone.utc).date().isoformat()

    if not GH_TOKEN:
        log("⚠️  未配置 GH_TOKEN，限额仅 60 次/小时，大概率会中途失败")

    store = load_json(REPOS_JSON, {"generated_at": None, "repos": []})
    seen = load_json(SEEN_JSON, {})
    manual = load_json(MANUAL_NOTES, {})
    by_name = {r["full_name"]: r for r in store.get("repos", []) if r.get("full_name")}
    log(f"本地已有 {len(by_name)} 条记录，{len(seen)} 条已生成说明，{len(manual)} 条已精校")

    # ---- 1. 候选池 -------------------------------------------------------
    hot_sel, fresh_sel = pick_candidates()
    selected = hot_sel + fresh_sel
    if not selected:
        log("❌ 候选池为空，终止（不改动任何数据）")
        return 1

    # ---- 2. 近 7 天增量 --------------------------------------------------
    # 统一口径：以昨日 UTC 为基准；接口数据通常滞后 1 天，两种截止日都算「当天已算过」
    anchor_date = datetime.now(timezone.utc).date() - timedelta(days=1)
    anchor = anchor_date.isoformat()
    reuse_ok = {anchor, (anchor_date - timedelta(days=1)).isoformat()}

    deltas: dict = {}
    for name, prev in by_name.items():
        if prev.get("stars_7d_asof") in reuse_ok:
            deltas[name] = (prev.get("stars_7d"), prev.get("stars_7d_asof"))

    pending = [r for r in selected if r["full_name"] not in deltas]
    if DELTA_N > 0:
        pending = pending[:DELTA_N]
    log(f"计算 {len(pending)} 个仓库的近 7 天增量（基准日 {anchor}，复用当日已有 {len(deltas)} 个）…")
    if pending:
        with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
            futures = [pool.submit(fetch_star_delta, r["full_name"], anchor) for r in pending]
            for fut in futures:
                name, delta, asof = fut.result()
                deltas[name] = (delta, asof)
    ok = sum(1 for r in selected if deltas.get(r["full_name"], (None,))[0] is not None)
    log(f"  增量数据可用 {ok}/{len(selected)}")

    # ---- 3. 挑出需要补写说明的新仓库 --------------------------------------
    need_notes = [r for r in selected
                  if r["full_name"] not in manual and r["full_name"] not in seen]
    log(f"需要补写说明的仓库：{len(need_notes)} 个")

    pend_items: list = []
    for i, repo in enumerate(need_notes, 1):
        readme = fetch_readme(repo["full_name"])
        pend_items.append({
            "full_name": repo["full_name"],
            "description": (repo.get("description") or "").strip(),
            "language": repo.get("language") or "",
            "stars": int(repo.get("stargazers_count") or 0),
            "topics": (repo.get("topics") or [])[:6],
            "readme": readme or (repo.get("description") or "")[:README_CHARS],
        })
        if i % 20 == 0:
            log(f"  README {i}/{len(need_notes)}")

    # ---- 3b. 可选：外部大模型兜底（只有配了 --with-llm 才走） -------------
    generated: dict = {}
    if pend_items and WITH_LLM and LLM_API_KEY and not NO_LLM:
        log(f"用外部模型生成说明（{len(pend_items)} 个）…")
        left = []
        for item in pend_items:
            repo = {"full_name": item["full_name"], "description": item["description"],
                    "language": item["language"], "stargazers_count": item["stars"]}
            result = classify_repo(repo)
            if result:
                generated[item["full_name"]] = result
            else:
                left.append(item)
            time.sleep(0.2)
        pend_items = left
        log(f"  成功 {len(generated)} 个，剩余待人工/智能体补写 {len(pend_items)} 个")

    # ---- 4. 写中转文件 ---------------------------------------------------
    items = []
    for repo in selected:
        name = repo["full_name"]
        delta, asof = deltas.get(name, (None, None))
        items.append({"repo": _slim(repo), "delta": delta, "asof": asof})

    write_json(CACHE_JSON, {
        "anchor": anchor,
        "today": today,
        "generated": generated,
        "items": items,
    })
    write_json(PENDING_JSON, {
        "anchor": anchor,
        "count": len(pend_items),
        "hint": (
            "把下面每个仓库写成 {full_name: {category, summary, usage}} 追加进 "
            "data/manual_notes.json。category 必须是固定枚举之一："
            + "、".join(CATEGORIES)
            + "。summary 为 1~2 句中文，说清干什么用；usage 为一句中文，说清怎么上手。"
        ),
        "repos": pend_items,
    })

    elapsed = time.time() - started
    log(f"✅ 采集完成：候选 {len(selected)} 个，增量 {ok} 个，"
        f"待写说明 {len(pend_items)} 个，耗时 {elapsed:.1f}s")
    log(f"   → {CACHE_JSON.relative_to(ROOT)} / {PENDING_JSON.relative_to(ROOT)}")
    return 0


# --------------------------------------------------------------------------
# 阶段二：合并写盘（说明来自 manual_notes.json，或 fetch 阶段的模型结果）
# --------------------------------------------------------------------------

def stage_apply() -> int:
    cache = load_json(CACHE_JSON, None)
    if not cache or not cache.get("items"):
        log("❌ 没有可用的采集结果（data/_cache.json 缺失），请先跑 --stage fetch")
        return 1

    anchor = cache["anchor"]
    today = cache["today"]
    generated = cache.get("generated") or {}

    store = load_json(REPOS_JSON, {"generated_at": None, "repos": []})
    seen = load_json(SEEN_JSON, {})
    manual = load_json(MANUAL_NOTES, {})
    long_notes = [n for n, v in manual.items()
                  if len(str(v.get("summary") or "")) > 80
                  or len(str(v.get("usage") or "")) > 30]
    if long_notes:
        log(f"  ⚠️  {len(long_notes)} 条说明偏长，可能撑破卡片：{', '.join(long_notes[:5])}")
    by_name = {r["full_name"]: r for r in store.get("repos", []) if r.get("full_name")}

    new_count = 0
    fallback = []
    for item in cache["items"]:
        repo = item["repo"]
        name = repo["full_name"]
        previous = by_name.get(name)
        record = build_record(repo, item.get("delta"), item.get("asof"),
                              previous, generated.get(name), manual.get(name), today)
        if previous is None:
            new_count += 1
        if record.get("generated_by") == "fallback":
            fallback.append(name)
        by_name[name] = record
        if name in manual or name in generated:
            prev_seen = seen.get(name) or {}
            seen[name] = {
                "first_seen": prev_seen.get("first_seen") or record["first_seen"],
                "category": record["category"],
            }

    repos = sorted(by_name.values(), key=lambda r: r.get("stars") or 0, reverse=True)
    # 各仓库的截止日应当一致；取出现次数最多的那个作为全局基准日
    asofs = Counter(it.get("asof") for it in cache["items"] if it.get("asof"))
    data_asof = asofs.most_common(1)[0][0] if asofs else anchor
    with_delta = sum(1 for r in repos
                     if r.get("stars_7d_asof") == data_asof and r.get("stars_7d") is not None)
    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "data_asof": data_asof,
        "stats": {
            "total": len(repos),
            "scanned": len(cache["items"]),
            "new_this_run": new_count,
            "with_delta": with_delta,
        },
        "repos": repos,
    }

    write_json(REPOS_JSON, payload)
    write_json(SEEN_JSON, dict(sorted(seen.items())))

    # 供本地双击打开 index.html 时使用（file:// 下 fetch 会被 CORS 拦截）
    js = "window.GH_TREND_DATA = " + json.dumps(payload, ensure_ascii=False) + ";\n"
    REPOS_JS.write_text(js, encoding="utf-8", newline="\n")

    log(f"✅ 写入完成：共 {len(repos)} 条记录，本次新增 {new_count} 个，"
        f"有增量 {with_delta} 个")
    if fallback:
        log(f"  ⚠️  {len(fallback)} 条仍在用官方描述兜底：{', '.join(fallback[:5])}"
            + (" …" if len(fallback) > 5 else ""))
    return 0


def main() -> int:
    stage = _arg_value("--stage", "all").strip().lower()
    if stage == "fetch":
        return stage_fetch()
    if stage == "apply":
        return stage_apply()
    if stage in ("all", "", "run"):
        rc = stage_fetch()
        if rc != 0:
            return rc
        return stage_apply()
    log(f"❌ 未知的 --stage {stage}，可选 fetch / apply / all")
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
