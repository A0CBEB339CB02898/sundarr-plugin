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

仓库目前处于初始化阶段，尚未交付真实插件。首个计划插件是 TMDb `CATALOG_PROVIDER`。

当前没有提交 `sundarr_plugin.toml`：Sundarr Manifest v2 要求至少包含一个真实、可激活的 `[[plugins]]` 声明。首个 TMDb 插件实现时将同时加入正式 Manifest，避免用不可运行的占位插件制造虚假可用状态。

## 目标结构

```text
sundarr-plugin/
├── sundarr_plugin.toml          # 首个真实插件落地时加入
├── plugins/
│   ├── tmdb_catalog/
│   ├── douban_catalog/
│   └── douban_watchlist/
├── src/
│   └── sundarr_official_plugins/
├── tests/
│   ├── offline/
│   └── live/
└── docs/
```

同一仓库中的插件共享 Git commit 和仓库级原子更新边界，但每个插件必须拥有独立 `plugin_id`、配置、启停、健康检查和错误状态。

## 开发原则

- 只依赖 Sundarr Core 公开插件合同，不导入 ORM、服务层或 Worker 私有实现。
- 默认 `pytest` 只运行离线 fixture 和合同测试。
- 实时测试通过显式命令运行，凭据只从环境变量读取。
- 插件开发必须使用真实数据持续回归 Core，但实时外部服务不能成为默认自动化测试依赖。
- TMDb 电影与剧集使用精确身份命名空间，例如 `tmdb.movie` 和 `tmdb.tv`，避免同号 ID 错误合并。

## 本地开发

要求 Python 3.12 或更高版本：

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python -m pytest
```

开发真实插件时，还需要让当前环境可以导入同版本 Sundarr Core 的公共合同。具体方式和实时测试命令将在首个 TMDb 插件交付时补充。

## 相关仓库

- [Sundarr Core](https://github.com/A0CBEB339CB02898/sundarr)
- [Sundarr Sources](https://github.com/A0CBEB339CB02898/sundarr-sources)
