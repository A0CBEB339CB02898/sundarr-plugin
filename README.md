# Sundarr Plugin

Sundarr 的非 SOURCE 官方插件集合仓库。

## 仓库职责

本仓库维护：

```text
CATALOG_PROVIDER：媒体目录、搜索、热门、分类和详情
WATCHLIST_PROVIDER：外部想看列表同步
未来由 Sundarr Core 正式支持的其他非 SOURCE 官方插件
```

本仓库不维护具体资源链接搜索源。SeedHub 等 `SOURCE` 插件继续放在独立的 [`sundarr-sources`](https://github.com/A0CBEB339CB02898/sundarr-sources) 仓库，以隔离更敏感的内容、分发和更新边界。

## 当前状态

仓库基线、TMDb 与豆瓣 `CATALOG_PROVIDER`、Manifest v2、离线合同测试和真实数据端到端验收均已完成。2026-09-14 的发布前加固将豆瓣目录统一到无密钥的移动端公开 JSON 路径：热门使用 subject collection，分类使用 recommend；关键词搜索优先使用 `rexxar/api/v2/search`，无有效影视项或结构异常时降级解析豆瓣电影搜索页的 `window.__DATA__`。所有入口都执行结构校验并排除广告、榜单卡片、图书和人物。实现不复制 Frodo 客户端中的逆向 API key 或签名密钥，也不承诺非官方接口具备官方 SLA。Core `/discover`、Web Console 数据来源切换、海报墙、详情、分页、失败隔离和启动恢复均须随正式锁定提交回归。Plugin API v2 保持冻结，正式发布锁定以 Sundarr Core 路线图记录为准。独立 `sundarr-sources` 仓库中的 SeedHub SOURCE v2 也已完成官方发布和 Core 锁定验收。豆瓣想看公开列表模式和分页结构已完成真实验证，当前实现独立 `douban-watchlist`。

## 目标结构

```text
sundarr-plugin/
├── sundarr_plugin.toml          # 通用 Manifest v2
├── plugin_entry.py              # 仓库根加载入口
├── src/
│   └── sundarr_official_plugins/
│       ├── tmdb_catalog/
│       ├── douban_catalog/
│       └── douban_watchlist/
├── tests/                       # 离线测试与显式实时测试
└── docs/                        # 插件规格和验收边界
```

同一仓库中的插件共享 Git commit 和仓库级原子更新边界，但每个插件必须拥有独立 `plugin_id`、配置、启停、健康检查和错误状态。

## 开发原则

- 只依赖 Sundarr Core 公开插件合同，不导入 ORM、服务层或 Worker 私有实现。
- 默认 `pytest` 只运行离线 fixture 和合同测试。
- 仓库级实时测试通过显式命令运行，凭据只从当前测试进程环境变量读取；这不是 Sundarr 正式运行时的配置方式。
- 插件开发必须使用真实数据持续回归 Core，但实时外部服务不能成为默认自动化测试依赖。
- TMDb 电影与剧集使用精确身份命名空间，例如 `tmdb.movie` 和 `tmdb.tv`，避免同号 ID 错误合并。
- 豆瓣目录使用 `douban.subject` 身份命名空间；缺少共同稳定外部 ID 时不按标题和年份静默合并到 TMDb。
- 豆瓣实现参考 MIT 许可的 [`Marvae/douban-cli`](https://github.com/Marvae/douban-cli) 的响应校验与网页搜索降级思路，但使用 Python 独立实现，不引入 Node.js 运行时或运行时仓库依赖。
- MoviePilot、Jellyfin 豆瓣插件等成熟实现证明 Frodo 路径可用，但其公开代码包含逆向客户端密钥；官方插件不得复制这些密钥，避免安全、授权与随时失效风险。

## 本地开发

要求 Python 3.12 或更高版本：

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python -m pytest
```

开发真实插件时，还需要让当前环境可以导入同版本 Sundarr Core 的公共合同：

```powershell
python -m pip install -e "..\sundarr"
```

默认离线测试：

```powershell
python -m pytest
```

TMDb 实时测试从环境变量读取 API Read Access Token：

```powershell
$env:TMDB_API_READ_ACCESS_TOKEN = "<仅当前终端使用>"
python -m pytest -o addopts= -m live
```

不要把 Token 写入 `.env`、测试参数、命令历史示例或提交文件。

上述环境变量只服务于不修改用户数据库的独立测试。正式运行时应在 Sundarr `/app/plugins` 中配置 `tmdb-catalog`，由 Core 配置 API 持久化到 `PluginConfig`；插件仓库本身不读取宿主 API / Worker 环境变量作为运行配置。

豆瓣目录实时测试访问无需账号的公开目录数据，不需要 Cookie 或 Token：

```powershell
python -m pytest -o addopts= -m live tests/test_douban_live.py
```

豆瓣想看实时测试只读取显式传入的公开数字用户 ID，不使用 Cookie：

```powershell
$env:DOUBAN_WATCHLIST_USER_ID = "<公开豆瓣用户 ID>"
python -m pytest -o addopts= -m live tests/test_douban_watchlist_live.py
```

实时响应不得未经审查直接保存为 fixture。默认测试使用经过裁剪的离线样本，避免把外部服务可用性变成普通回归测试前提。

## 相关仓库

- [Sundarr Core](https://github.com/A0CBEB339CB02898/sundarr)
- [Sundarr Sources](https://github.com/A0CBEB339CB02898/sundarr-sources)
