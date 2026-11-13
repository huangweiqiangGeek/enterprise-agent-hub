# D13 GitHub 只读连接实施记录

日期：2026-11-14（UTC+08）。基于 D12 `a427a61`，用户要求逐项完成后提交推送。

## 状态与范围

固定 MCP 适配、连接与仓库管理、工具启停及页面已实现；**真实 GitHub MCP 仍未验证，D13 不能标记完整业务验收**。专用凭据未配置，未复用 Git 推送账号或其他项目凭据。本轮不推进 D14。

- AP39～AP46：管理员测试/保存固定连接，成员只读可用性；凭据输入不回填，保存后清空。候选失败不覆盖有效连接；已保存凭据测试失败将连接标为不可用。
- 明确 owner/repo 与向组织共享确认，服务端精确匹配 full_name、保存稳定 repository ID；空搜索或其他仓库不会授权。仓库软撤销，更换凭据停用全部旧仓库；原幂等请求不能恢复撤销授权。
- 固定工具注册表、组织启停、Agent readiness/复制剔除失效工具；知识检索不可关闭。内部读工具入口前后复核成员/Agent/工具/连接/仓库与 accessRevision，无通用HTTP执行入口。
- 官方 Streamable HTTP、固定 endpoint、Bearer 与只读/精确工具 headers；不跟随重定向、不提供 REST fallback。30秒调用范围、1MiB响应上限；方法仅get，查询与仓库/日期限定由代码构造。
- 工具字段类型/必填/只读注解检查；协议与 input/output Schema 指纹保存并在后续调用比对，变化拒绝。写工具即使出现在远端列表也不会注册。
- 搜索最多每页20条、page1～10；日期级扩大查询后按实际UTC updatedAt半开区间过滤。来源校验主机/仓库/类型/编号，时间与字段归一，正文限制8000字符并披露。真实服务格式兼容性仍须联调。
- 0010_github 新增三张组织隔离表及审计。网络请求不占组织数据库锁；返回后复核版本/授权，竞态拒绝或幂等返回。非空配置禁止破坏性降级。

## 验证

所有集成测试仅使用临时PostgreSQL数据库与S3桶；MCP fixture是合成JSON-RPC协议响应，不是GitHub抓包，也不计入真实验收。

- 完整Python回归：170 passed / 4 skipped，324.43秒，退出0。4项真实提供方/Linux测试未开启。
- 随后补充坏JSON/大小/类型/URL/时间和PR精确时间边界，最终D13专项21 passed，12.29秒，退出0；包括真实PostgreSQL、MCP SDK握手与工具调用、认证/权限/限流/重定向/Schema变化、精确仓库、跨组织、重复/并发、返回时撤权、无凭据DTO/审计、安全降级。
- 前端单元8 passed；Nuxt构建/类型、ESLint/Prettier、Ruff check/format、mypy（109文件）、OpenAPI生成漂移检查通过。
- GitHub生产浏览器专项通过：凭据不回显/清空、候选测试失败保留原连接、明确共享、刷新、移除确认取消/接受、工具停用与390px窄屏。截图人工查看，微调复选框宽度与面板间距。
- 全量浏览器复验曾发现团队页SSR按钮过早可点，以及旧来源断言遗漏合法标题片段。团队页增加挂载前禁用；来源测试保持文档ID验证并接受三种确实存在的片段，不强求等分片段顺序。中间失败不算最终通过，最终结果见下方追加记录。

## 官方依据与复现

使用现有锁定依赖 MCP Python SDK 1.30.0、httpx 0.28.1；未增加依赖。配置依据[GitHub官方远程服务](https://github.com/github/github-mcp-server/blob/main/docs/remote-server.md)与[官方工具目录](https://github.com/github/github-mcp-server)，于本轮核对。官方源码/文档不是本账号真实连接成功证据。

```powershell
[Console]::OutputEncoding=[Text.UTF8Encoding]::new()
$env:EAH_TEST_ADMIN_URL='postgresql://eah:eah_dev@127.0.0.1:5432/postgres?connect_timeout=5'
$env:EAH_TEST_S3_ENDPOINT='http://127.0.0.1:4566'
uv run --locked pytest tests/test_github.py -q
uv run --locked pytest -q
uv run ruff check backend scripts tests
uv run ruff format --check backend scripts tests
uv run mypy
uv run python -m scripts.export_openapi --check
pnpm typecheck; pnpm lint; pnpm format:check; pnpm test:unit; pnpm build
$env:EAH_BROWSER_GITHUB='1'; $env:EAH_BROWSER_QA='1'; $env:EAH_BROWSER_INGESTION='1'
$node=(pnpm exec node -p process.execPath).Trim()
uv run python -m scripts.browser_smoke --node $node --model-fixture --storage
uv run python -m scripts.probe_github
```

`probe_github`本轮返回UNVERIFIED/退出2：缺少EAH_PROBE_GITHUB_TOKEN/OWNER/REPO。后续在本机安全配置专用只读凭据与明确测试仓库，执行真实initialize/tools-list/search-repositories调用，补录生产Schema指纹、服务版本及Issue/PR格式、限流/分页行为。合成fixture不替代这一项。

## 部署与后续边界

主体验库未迁移，保留0008及既有数据。部署D12/D13需显式升级至0010并更新API/Web、D12 worker，不声称本轮已部署。临时验收服务/数据库/桶由测试脚本清理，不关闭用户服务或清空卷。

D14仍需将GitHub读工具接入持久run预算、步骤、来源与T3总结；D17完成GitHub历史资料撤权投影。当前progress运行入口仍不开放。真实T2评测、真实MCP、外部S3等既有未验证项继续保留。


## 最终复验

- 最终生产构建全量浏览器：21 passed（1.3分钟），退出0；团队SSR/来源顺序两项修正均在此轮通过。最终临时API/Web端口49268/49269已释放，临时数据库/桶已清理。
- 最终D13专项再次21 passed（12.50秒），退出0；最后的字段防御修改另经Ruff格式/检查及mypy复核通过。全部客户端类型/格式/构建与单元检查已完成。
- 主体验库只读核查仍为0008_conversations；本轮没有部署或调用真实GitHub MCP。仓库没有GitHub Actions工作流文件，不将本地结果写作远端CI通过。
