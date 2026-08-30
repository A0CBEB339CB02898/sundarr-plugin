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

仓库基线已经完成，当前正在交付首个真实插件 TMDb `CATALOG_PROVIDER`。实现与验收边界见 [`docs/01-TMDb目录插件.md`](docs/01-TMDb目录插件.md)。

正式 `sundarr_plugin.toml` 将与可激活的 TMDb 实现一并加入，避免用不可运行的占位声明制造虚假可用状态。

## 目标结构

```text
sundarr-plugin/
├── sundarr_plugin.toml          # 通用 Manifest v2
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
python -m pytest -m live
```

不要把 Token 写入 `.env`、测试参数、命令历史示例或提交文件。

## 相关仓库

- [Sundarr Core](https://github.com/A0CBEB339CB02898/sundarr)
- [Sundarr Sources](https://github.com/A0CBEB339CB02898/sundarr-sources)
