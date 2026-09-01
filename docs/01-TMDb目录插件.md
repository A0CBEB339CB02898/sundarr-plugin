# TMDb 目录插件

本文档定义 `tmdb-catalog` 的平台映射、配置、分页和验收边界。插件类型为 `CATALOG_PROVIDER`，只提供媒体目录数据，不创建任务、不访问数据库，也不承担具体资源链接搜索。

## 1. 配置

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `api_read_access_token` | password | 是 | 无 | TMDb API Read Access Token，使用 Bearer 认证 |
| `language` | string | 否 | `zh-CN` | TMDb 返回内容语言 |
| `include_adult` | boolean | 否 | `false` | 是否包含成人内容，默认关闭 |
| `image_size` | select | 否 | `w500` | 海报和背景图尺寸 |

正式运行 Token 只能通过 Sundarr Web Console / Core 配置 API 保存在 `PluginConfig` 中，Manifest、fixture、日志和异常不得包含真实值。Core 负责敏感字段静态加密和脱敏；插件不能直接访问数据库。

插件能力描述必须提供 TMDb 来源署名：链接到 `https://www.themoviedb.org`，使用 TMDb 官方批准的标识，并展示官方要求的非背书声明。Sundarr 仅把这些值作为通用 `CatalogAttribution` 返回，Core 不硬编码 TMDb 专用字段。运营者仍需根据项目用途自行确认适用许可；未来 AI Tool API 不得自动把 TMDb 数据用于模型训练或未经复核的 AI 场景。

## 2. 官方接口映射

| Sundarr 操作 | TMDb v3 接口 |
|---|---|
| 搜索全部 | `/search/multi`，过滤 person |
| 搜索电影 | `/search/movie` |
| 搜索剧集 | `/search/tv` |
| 热门电影 | `/trending/movie/day` |
| 热门剧集 | `/trending/tv/day` |
| 分类电影 | `/discover/movie` |
| 分类剧集 | `/discover/tv` |
| 电影详情 | `/movie/{id}` |
| 剧集详情 | `/tv/{id}` |
| 电影题材 | `/genre/movie/list` |
| 剧集题材 | `/genre/tv/list` |
| 地区选项 | `/configuration/countries` |
| 健康检查 | `/configuration` |

认证使用 `Authorization: Bearer <token>`。图片 URL 按 TMDb 官方规则由 `https://image.tmdb.org/t/p/{size}{file_path}` 组成；MVP 不下载海报二进制文件。

参考：

- [TMDb 应用认证](https://developer.themoviedb.org/docs/authentication-application)
- [TMDb 多类型搜索](https://developer.themoviedb.org/reference/search-multi)
- [TMDb 电影发现](https://developer.themoviedb.org/reference/discover-movie)
- [TMDb 剧集发现](https://developer.themoviedb.org/reference/discover-tv)
- [TMDb 图片规则](https://developer.themoviedb.org/docs/image-basics)

## 3. 身份和字段映射

电影：

```text
external_id_provider = tmdb.movie
external_ids = {"tmdb.movie": "<id>"}
title = title
original_title = original_title
release_date = release_date
```

剧集：

```text
external_id_provider = tmdb.tv
external_ids = {"tmdb.tv": "<id>"}
title = name
original_title = original_name
release_date = first_air_date
```

TMDb 电影和剧集 ID 只在各自子域内解释，不得合并成笼统的 `tmdb` 命名空间。详情中的 `imdb_id` 可附加为 `imdb` 外部 ID；缺失时不生成空值。

公共字段：

- `year` 从有效发布日期提取；无有效日期时为 `None`。
- `genres` 优先使用详情的题材名称；列表响应的 `genre_ids` 通过已加载题材表映射。
- `regions` 使用 `origin_country`，电影详情缺失时使用 `production_countries[].iso_3166_1`。
- `rating` 使用 `vote_average`，`vote_count` 原样映射为非负整数。
- `poster_url`、`backdrop_url` 和 `image_urls` 只在路径有效时生成。

## 4. 筛选与排序

插件全局能力并集包含媒体类型、题材、地区和年份筛选，以及热度、评分和上映时间排序；实际能力按操作声明：

| 操作 | 筛选 | 排序 |
|---|---|---|
| `search` | 媒体类型、题材、年份 | 不支持 |
| `trending` | 媒体类型 | 不支持 |
| `categories` | 媒体类型、题材、地区、年份 | 热度、评分、上映时间 |

TMDb 电影文本搜索的 `region` 参数控制地区发行日期展示，不等于 Sundarr `region` 的影片来源地区；剧集文本搜索也没有等价来源地区参数。因此插件不能把分类接口的地区筛选声明给 `search`，也不能用当前页本地排序冒充平台级搜索排序。

`include_adult=false` 必须同时作为本地结果保护应用于所有列表操作。search / discover 可以继续传递 TMDb 对应参数；trending 没有该查询参数，插件必须过滤响应中 `adult=true` 的候选，不能让首页热门区绕过配置。

`categories()` 通过 discover 接口映射：

```text
genres      -> with_genres（逗号连接，AND 语义）
regions[0]  -> with_origin_country
year_from   -> primary_release_date.gte / first_air_date.gte
year_to     -> primary_release_date.lte / first_air_date.lte
sort        -> popularity.desc / vote_average.desc / 日期.desc
```

`search()` 使用搜索接口，并对搜索响应执行明确的本地筛选。不能可靠判断某项筛选时，该候选不得通过筛选，不能静默忽略条件。

`CatalogQuery.genres` 可以包含多个题材。分类接口必须把全部值传给 TMDb；文本搜索的本地过滤也必须要求候选包含全部题材，不能只读取第一个值。地区仍只接受一个值。

## 5. 分页

Core 只看到不透明 `continuation_token`。插件内部 Token 包含版本、操作、查询摘要、TMDb 页码和页内偏移：

- Token 与当前查询不匹配时明确报错。
- `limit < 20` 时可以从同一 TMDb 页继续。
- 搜索和分类在 `limit > 20` 时最多请求满足 limit 所需的连续页面。
- TMDb trending 接口没有声明远程 `page` 参数；插件只允许在该次返回的结果数组内继续，不请求伪造的第 2 页，耗尽后返回 `None`。
- 到达 `total_pages` 或没有剩余结果时返回 `None`。

Token 不包含 API Token、完整请求 URL 或用户隐私数据。

## 6. 错误边界

- 401/403：认证失败，错误信息不得回显 Token。
- 404：详情不存在，返回可诊断异常。
- 429：限流，交给 Core 降级或重试策略，不在插件内部无限重试。
- 响应不是对象、缺少分页结构或字段类型非法：返回明确平台响应错误。
- 单条媒体字段异常应尽量隔离；无法形成 `CatalogItem` 的候选跳过并记录脱敏日志。

## 7. 测试

默认离线测试覆盖：

- Manifest v2、配置 schema 和 Activation。
- 电影/剧集搜索、热门、分类、详情映射。
- `tmdb.movie` / `tmdb.tv` 身份隔离。
- 题材、地区、年份和排序参数。
- 多页、页内偏移、Token 查询绑定和末页。
- 空结果、无图片、无日期、无评分和非法响应。
- Token 不进入日志与错误。

显式实时测试包含一条不需要凭据的真实端点认证失败冒烟，用于验证网络、TLS、Core HTTP 客户端、错误映射和脱敏；有效数据测试使用当前测试进程的环境变量 `TMDB_API_READ_ACCESS_TOKEN`，覆盖搜索、热门、分类、详情、Core conformance runner，以及插件经 Activation / Registry 后的 Core `/discover` API 与数据库身份归一化。该环境变量只用于隔离测试，不是 Sundarr 正式运行配置入口。默认 `pytest` 通过 marker 排除实时测试；显式执行命令为 `python -m pytest -o addopts= -m live`。没有 Token 时有效数据测试必须明确跳过，不能以认证失败冒烟或 fixture 代替里程碑真实验收。Web Console 海报墙和详情页仍需使用由 `/app/plugins` 保存的同一真实 Provider 配置单独做浏览器冒烟，API 测试不能替代页面验收。

## 8. 里程碑验收

本里程碑已于 2026-08-31 通过：

- `sundarr-plugin` 默认离线测试 `17 passed / 2 deselected`。
- 真实 TMDb 测试 `2 passed`，包括有效数据与无效认证路径。
- Core 从锁定 commit `f83f43d` 激活 `tmdb-catalog`。
- `/discover/providers`、搜索、热门电影/剧集、题材/地区/年份分类、详情和分页续页使用真实数据通过。
- `/app/discover` 海报墙、URL 查询状态和详情页使用真实数据通过，海报没有加载失败。
- Provider 禁用后详情返回 PostgreSQL 最小快照并标记 `degraded=true`，恢复启用后重新进入 active。
- 正式 Token 由 `/app/plugins` 写入 `PluginConfig`，敏感配置使用数据库外主密钥静态加密，API 只返回脱敏值。
- Core 全量测试 `265 passed`，前端生产构建通过。
- Plugin API v2 已冻结；后续必须保持向后兼容，破坏性变更需要新的协议版本。
