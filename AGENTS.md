# Agent 工作规则

本仓库是 Sundarr 的非 SOURCE 官方插件集合。项目相关文本原则上使用简体中文，代码标识符和外部协议字段除外。

## 仓库边界

```text
允许：CATALOG_PROVIDER、WATCHLIST_PROVIDER，以及后续经 Core 确认的其他非 SOURCE 官方插件。
禁止：SOURCE 插件、具体资源链接搜索实现、Sundarr ORM、数据库 Session、Worker 私有函数和 SMB 内部对象。
SOURCE 官方实现只进入 https://github.com/A0CBEB339CB02898/sundarr-sources.git。
```

每个插件必须通过 Sundarr Core 的公共合同和 `PluginContext` 接入。不得为单一平台要求 Core 增加平台专用分支。

## 开发顺序

1. 先更新 README、插件说明和长期架构决策。
2. 再实现插件、配置 schema、离线 fixture 和测试。
3. 默认测试必须离线、稳定、可重复，不访问实时外部服务。
4. 实时访问必须放入显式集成测试，并从环境变量读取凭据。
5. 每个真实插件新增或修改后，必须运行实时集成测试，并回归 Sundarr Core 的公共 conformance runner、API 和受影响的 Web Console 主路径。
6. 首个 TMDb 搜索、热门、分类、详情和海报墙端到端通过前，不得宣称 Plugin API v2 已冻结。

## 安全规则

不得提交 API key、Cookie、Token、账号、响应中的个人信息或其他凭据。日志、异常、测试快照和 fixture 必须脱敏。实时响应只能用于显式集成测试，不得未经审查直接固化为 fixture。

## Git 与验收

完成一个清晰、可验证的交付单元后创建小型提交，提交信息使用简体中文。提交前检查 `git status`、`git diff`、测试结果和敏感内容。默认分支为 `master`；未经用户明确授权不 push，不使用破坏性 Git 命令。

开始每个非 trivial 任务前，先简要汇报当前目标、当前进度、本轮交付物、验收标准和停止条件。
