# GitHub 趋势雷达

一个自用的 GitHub 项目趋势工具：每天扫描活跃仓库，按「总 Star」和「近 7 天增速」两个维度出榜，每个条目都带领域标签、中文功能说明、上手方式和商用许可标注。

纯 Python 标准库 + 单文件静态页面，无第三方依赖，零构建步骤。

## 目录结构

```
gh-trend-radar/
├── scripts/collect.py      # 采集器：搜索 → 算增量 → 抓 README → 合并写盘
├── scripts/publish.py      # 提交并推送到 GitHub 仓库
├── data/
│   ├── repos.json          # 主数据文件（页面读取这个）
│   ├── repos.js            # 同一份数据的 JS 版本，供本地双击打开时使用
│   ├── manual_notes.json   # 说明文本库（领域 / 作用 / 上手方式），永不覆盖
│   └── seen_repos.json     # 已写过说明的仓库名单，用于增量去重
├── index.html              # 静态页面
└── .env                    # GH_TOKEN 等密钥，已被 .gitignore 忽略
```

## 每日运行流程

分成两个阶段，中间由智能体补写说明，这样不需要额外的模型 API Key：

```bash
# 阶段一：采数据，并列出需要补写说明的新仓库
python scripts/collect.py --stage fetch

# 阶段二：（智能体读 data/_pending.json，把说明写进 data/manual_notes.json 后）
python scripts/collect.py --stage apply

# 阶段三：推送到 GitHub
python scripts/publish.py
```

`python scripts/collect.py` 不带参数等价于 `fetch + apply` 连跑，不调模型——适合只要更新增量数据、没有新仓库的日子。

如果确实想用外部模型自动兜底，加 `--with-llm` 并配置 `LLM_API_KEY`。

## 环境变量

密钥统一放在项目根目录 `.env`（一行一个 `KEY=VALUE`），脚本会自动读取：

```bash
GH_TOKEN=ghp_xxx                    # 必填，需要 repo 读写权限
GH_REPO=wangjiaweiing/desktop-tutorial
GH_BRANCH=main

# 以下可选
LLM_API_KEY=sk-xxx                  # 只有 --with-llm 时才用到
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat
```

| 变量 | 必需 | 默认 | 说明 |
|---|---|---|---|
| `GH_TOKEN` | 是 | — | 不带 token 限额只有 60 次/小时，跑不完 |
| `GH_REPO` | 否 | `wangjiaweiing/desktop-tutorial` | 推送目标仓库 |
| `POOL_PAGES` | 否 | `5` | 候选池页数，5 × 100 = 500 |
| `LIST_N` | 否 | `120` | 写入项目列表的仓库数 |
| `DELTA_N` | 否 | `0`（全部） | 实际算增量的仓库数，配额紧张时可调小 |
| `NEW_N` | 否 | `30` | 额外纳入的近 30 天新建项目名额 |
| `CONCURRENCY` | 否 | `4` | 并发数 |

## 说明文本的三层来源

优先级从高到低：

1. **说明库** `data/manual_notes.json` —— 精写的领域标签、功能说明、上手方式，**永不覆盖**
2. **外部模型** —— 仅在 `--with-llm` 时使用，结果记入 `seen_repos.json`
3. **官方描述兜底** —— 前两者都没有时临时顶上，`--stage fetch` 下次会自动把它重新列进待写清单

给某个仓库补说明，往 `manual_notes.json` 里加一条即可：

```json
"owner/repo": {
  "category": "前端开发",
  "summary": "一两句说清它干什么用。",
  "usage": "怎么上手，一句话。",
  "by": "agent"
}
```

`category` 必须是固定枚举，写了别的值会被强制改判为「其他」：

> AI/机器学习、前端开发、后端/服务端、数据库、DevOps/运维、命令行工具、移动开发、桌面应用、浏览器扩展、安全、数据分析、爬虫/自动化、文档/笔记、设计/UI、区块链、游戏开发、学习资源、其他

## 定时任务

每天由 WorkBuddy 的定时任务驱动，流程即上面三步。任务只在本地/智能体侧执行，不依赖 GitHub Actions（`.github/workflows/daily.yml.disabled` 是早期版本，已停用）。

## 工作原理

**候选池**：`stars:>50 pushed:>近30天`，按 star 降序翻 5 页拿到 500 个，取前 120 写入列表；再补 30 个「近 30 天新建且 star>50」的项目。补这 30 个是因为纯按总 star 排序的话，榜单会被 react、vscode 这类巨无霸占满，而刚开源一周暴涨几千星的新项目永远进不来——那恰恰是趋势工具最该抓的。

**近 7 天增量**：调用 `GET /repos/{owner}/{repo}/stargazers/history?per_page=3`，接口按自然周返回 `[{week, total, days[7]}]`（最新一周在前），把 3 周的 `days` 拼成日序列后统计最近 7 个完整日的和。**每个仓库只消耗 1 次请求**。

> 老办法是翻 `/stargazers` 列表逐页数，10 万星的仓库要翻 1000 页。现在不需要了。

**商用标注**：直接用搜索结果里自带的 `license.key`，零额外请求。

| license.key | 标注 |
|---|---|
| `mit` / `apache-2.0` / `bsd-2-clause` / `bsd-3-clause` | ✅ 允许商业二次开发 |
| `gpl-2.0` / `gpl-3.0` / `agpl-3.0` | ⚠️ 可商用但衍生代码需开源 |
| `null` / `noassertion` | ❌ 未声明许可证，默认不可商用 |
| 其他（`other`、`cc-by-4.0` 等） | ❓ 需人工确认 |

**增量更新**：`seen_repos.json` + `manual_notes.json` 一起构成「已处理」判据。每次只对两边都没有的仓库抓 README、写说明。老数据一律保留不删，仓库掉出候选池后仍留在数据文件里。当天已算过的增量会复用（`stars_7d_asof` 比对），重复运行不浪费配额。

## 成本

| 项目 | 每日消耗 |
|---|---|
| GitHub API | 约 300 次（5 搜索 + 150 增量 + 新仓库 README），占 5000/h 限额的 6% |
| 运行时长 | 1~3 分钟 |

## 免责声明

许可证信息来自 GitHub API 的自动识别结果，仅供参考。部分仓库存在多许可证、附加条款或自定义协议，商用前请务必自行核实仓库 LICENSE 文件全文及依赖链上的许可证义务。
