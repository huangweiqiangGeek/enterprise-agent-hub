# Enterprise Agent Hub

面向研发团队的 AI Agent 工作平台，整合团队知识库、授权检索、Agent 配置与可观测的任务执行。

当前已实现 D01～D11：工程基础、身份/组织/权限、模型配置/限制、可靠文件接受、文档解析入库、授权混合检索、Agent 配置与不可变版本，以及持久化文档问答和可操作的对话工作台。支持会话搜索/重命名、原运行断线恢复、来源抽屉、步骤、Markdown导出与追问，支持停止运行、硬时限、有限进程恢复、人工关联重试，以及执行记录和分项用量统计；真实百炼与Linux队列验收单列。需求对比/项目进展由后续任务开放，外部S3/GitHub MCP尚未验证。证据见 [D11记录](docs/D11-执行记录与用量实施记录.md)、[D10记录](docs/D10-取消与进程恢复实施记录.md)、[D09记录](docs/D09-对话与恢复实施记录.md)、[D08记录](docs/D08-持久运行与文档问答实施记录.md)；产品范围见 [PRD](docs/PRD.md)。

开发前从 [AGENTS.md](AGENTS.md) 按任务读取 [docs 开发规范](docs/README.md)。

## 快速启动

使用问答：登录 → 选择组织 → 准备模型与已就绪知识资料 → 启用问答Agent → “对话工作台”。选择资料范围后发送，刷新/断网恢复同一运行；来源按引用读取，成功结果可下载含范围与限制的Markdown。普通成员也能发起自己的任务，管理员审计详情始终只读。完整验收与复现命令见[D09记录](docs/D09-对话与恢复实施记录.md)。

运行时可以“停止运行”，服务器确认终态前显示“正在停止”。失败、取消或超时后可“准备重试”，核对当前配置与资料后手动创建关联的新运行。恢复不延长原时限；部分模型流中断不会重新生成拼接。行为、进程故障验收与升级说明见[D10记录](docs/D10-取消与进程恢复实施记录.md)。

侧栏“执行记录与用量”可按状态、Agent、日期和时区筛选，查看历史配置、来源与关联重试。管理员可切换组织只读审计。统计区分聊天、Embedding、Rerank及实测/估算/不可得；成功率仅统计终态。D11不增加迁移，沿用0008数据库和qa-v2 worker，只需更新API/Web。

适用于首次启动工程骨架。需要 pnpm 12.14.0、[uv 0.13.7](https://docs.astral.sh/uv/getting-started/installation/) 和可运行 Linux 容器的 Docker Desktop/Engine（含 Compose）。pnpm 会按锁文件下载项目 Node 24.22.2；uv 会读取 `.python-version` 使用 Python 3.12.16。不会要求切换电脑上其他项目的默认 Node。

以下命令均从仓库根目录执行。Windows 使用 PowerShell；Linux/macOS 的首次配置复制命令为 `cp .env.example .env`。

```powershell
pnpm install --frozen-lockfile
uv sync --locked
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
docker compose --profile storage up -d --wait
uv run alembic upgrade head
uv run python -m backend.checkpoints
uv run --locked python -m scripts.init_storage
docker compose --profile storage --profile ingestion build ingestion
docker compose --profile storage --profile ingestion --profile runs up -d ingestion dispatcher runs
```

初始化顺序必须是 **依赖健康 → Alembic → checkpoint setup → 存储桶初始化 → ingestion/dispatcher/runs → API/Web**。Alembic 创建 `app`、`checkpoints` schema 和 `vector` 扩展；LangGraph 只管理 `checkpoints` 内的适配器表。checkpoint 和存储桶初始化可以重复执行，未完成 Alembic 时 checkpoint 会拒绝运行。API/worker 启动不会自动改库或建桶。

分别在两个根目录终端启动：

```powershell
# 终端一：API
uv run python -m backend

# 终端二：Web
pnpm dev
```

打开 [注册页](http://127.0.0.1:3300/register)，注册后创建组织，或由管理员在“团队成员”按注册邮箱核对并添加。已有账号从 [登录页](http://127.0.0.1:3300/login) 进入；“账号设置”可修改显示名和密码，修改密码会撤销全部登录会话。不自动导入示例账号。第二位成员可用浏览器无痕窗口注册，体验角色与组织隔离。

[服务状态页](http://127.0.0.1:3300/status) 保留 D01 健康检查；点击“重新检查”，正常显示“服务与数据库已就绪”。进入组织后，侧栏提供“知识库”“模型配置”和“组织限制”。健康端点检查数据库/checkpoint，不代表 S3 或模型已可用；上传仍单独检查实际依赖。

模型配置由所有者/管理员操作：选择服务协议和部署允许的地址，填写模型名和 API Key，点击“仅测试”查看结果，或“测试并保存”重新测试并生效。聊天测试最多两次调用，Embedding/Rerank 各一次，可能产生用量；失败保留旧配置。凭据不读回，留空表示保留已保存凭据，更换协议或服务地址时必须重新填写。普通成员只见已启用的模型名称。新环境没有预置真实账号或假成功配置。

聊天默认支持 OpenAI 协议的 `gpt-4o-mini-2024-07-18`、`gpt-4o-2024-08-06`，以及百炼协议的 `qwen-plus` 非思考模式；每种协议使用已验证的 tokenizer 映射，账号权限仍需实际测试。百炼选型为 `qwen-plus`、`text-embedding-v4`（1024 维）、`gte-rerank-v2`；Embedding 使用 `openai-embeddings`，Rerank 使用 `dashscope-rerank`。不承诺其他兼容服务均可用。已有文档或处理任务时 Embedding 模型/地址/维度不可变；同语义可轮换凭据。组织限制页显示系统硬上限，管理员可保存不超过硬值的正整数；设置仅作用于新接受任务。

```powershell
curl.exe -f http://127.0.0.1:8300/api/v1/health/live
curl.exe -f http://127.0.0.1:8300/api/v1/health/ready
docker compose exec redis redis-cli ping
```

前两项正常返回 200；Redis 返回 `PONG`。`live` 只代表 API 存活；数据库或初始化未就绪时，`ready` 返回 503。即使依赖尚未启动，也可以打开页面查看这个失败状态。

停止 Web/API 用各自终端的 `Ctrl+C`；停止本项目服务用 `docker compose --profile storage --profile ingestion --profile runs stop`，保留数据库和文件卷。

## 知识库与上传

所有者/管理员新建知识库，支持名称/说明编辑、搜索与分页；成员可以查看和下载。上传前先测试并保存 Embedding 配置，用于固定此次处理的模型版本；缺配置会提示，不会启用假模型。PDF、UTF-8 TXT/MD 逐文件校验，默认上限 20 MiB，实际以组织限制为准；同名文件独立保存。

上传进度 100% 只表示传输完成。S3 对象写入成功且数据库提交文档、处理尝试、outbox 和幂等记录后才返回接受；响应丢失自动查询原请求，重试保留原标识。取消传输不能撤回已接受文件，可使用“删除文档”。后台依次解析、切片和调用真实 Embedding，全部成功才显示“已就绪”。损坏、加密、扫描无文本 PDF 分别给出失败原因，不支持 OCR；管理员可重新处理，历史尝试保留。成员可看当前提取文本，管理员另可预览片段。

本地 S3 使用固定版本 SeaweedFS，`127.0.0.1:4566` 映射 S3 端口，文件和元数据保存在 `s3_objects` 卷。`EAH_S3_ENDPOINT/REGION/BUCKET/ACCESS_KEY/SECRET_KEY/PREFIX` 配置见 `.env.example`；本地 test/test 仅对应此开发服务。`EAH_MAX_DOCUMENTS_PER_ORGANIZATION` 默认 1000。对外部署需提供实际私有 S3 服务并另行验收。

Compose dispatcher 每 5 秒核对 PostgreSQL 状态并投递 outbox，每批最多 100 项；失败有限退避，五次后需下列运维入口显式重试。删除立即使文件不可访问，清理任务删除原文/提取文本并清空片段正文与向量，保留定位记录。无数据库引用且超过 24 小时的对象每小时分页检查清理。下面也保留单批运维命令。

```powershell
uv run --locked python -m backend.workers.outbox
uv run --locked python -m backend.workers.outbox --retry-failed
uv run --locked python -m backend.workers.outbox --orphans
# 如输出 nextCursor，下一批用 --orphans --cursor <返回值>
```

dispatcher 补投超过 30 秒仍 queued 的记录，worker 按当前 attempt 和租约领取；已过期 processing 明确失败，用户重试创建新 attempt，不自动重放未知结果的模型请求。默认单个 Linux worker 并发 2、每组织 1；不要自行扩容副本来突破此开发上限。解析子进程限制 512 MiB、30 秒 CPU/60 秒墙钟，整次入库 600 秒。每文档最多 500 页、200 万字符、4096 片段；组织最多 50000 有效片段。切片最多 384 UTF-8 字节、重叠最多 48 字节，不冒充模型精确 token 数；真实调用用量单独落库。

开发 worker 默认 DNS `223.5.5.5`，可通过 `EAH_WORKER_DNS` 改为部署网络可达的解析器。本机 Docker 默认 DNS 曾混入代理 fake-IP，应用仍拒绝保留地址，不以关闭校验解决。镜像不会复制 `.env`，使用数据库中的组织模型版本；生产网络、外部 S3、负载和 RAG 质量需后续验收。

```powershell
# 显式运行会调用真实百炼的 Linux 验收；使用独立临时 DB/桶/队列/容器
$env:EAH_TEST_ADMIN_URL='postgresql://eah:eah_dev@127.0.0.1:5432/postgres'
$env:EAH_RUN_LINUX_INGESTION='1'
uv run --locked pytest tests/test_linux_ingestion.py -q -s
# Docker 不在 PATH 时设置 EAH_DOCKER 为 docker.exe 的绝对路径
# 浏览器解析/预览/重试验收复用下方 browser_smoke，另设置：
$env:EAH_BROWSER_INGESTION='1'
```

## 持久文档问答 API

D08 先实现后端 T1 问答；聊天页面和 Agent 试运行入口在 D09 接入。需启用且已绑定就绪知识库、`knowledge.search` 和聊天模型的 QA Agent。使用已登录 Cookie、`X-Organization-Id`，写请求另带 `Origin`、`X-CSRF-Token` 与不同操作的 `Idempotency-Key`：

1. `POST /api/v1/conversations`，提交 `{agentId,taskType:"qa",title?}`，返回 201 会话。
2. `POST /api/v1/conversations/{id}/runs`，提交 `{agentId,input:{question,knowledgeBaseIds,documentIds?}}`，返回 202；接受后即可查询，模型调用在 Linux worker 中执行。
3. `GET /api/v1/runs/{runId}` 查看排队、运行或终态、部分文本、最终结构及引用；`GET /api/v1/runs/{runId}/steps` 查看各次调用和计量。
4. 接受响应丢失时，用原键查询 `/api/v1/idempotency-requests/{key}?operation=run.create&resourceId={conversationId}`，或按原键和相同内容重发；同会话同时只接受一个非终态任务。

`eventsUrl` 预留给 D09，当前不提供事件订阅。步骤和事件已入库；无证据可以成功返回资料不足，伪造引用校验失败不会发布成功答案。租约丢失明确失败，未知请求不自动拼接重生成。聊天、Embedding、Rerank 计量分开，未知 usage 保留预留或 unavailable，不冒充零费用。

```powershell
$env:EAH_TEST_ADMIN_URL='postgresql://eah:eah_dev@127.0.0.1:5432/postgres'
$env:EAH_TEST_S3_ENDPOINT='http://127.0.0.1:4566'
uv run --locked pytest tests/test_runs.py tests/test_run_migration.py -q
# 显式真实调用；需已构建 eah-worker:local，读取已有 EAH_PROBE_API_KEY
$env:EAH_RUN_REAL_QA='1'
uv run --locked pytest tests/test_real_qa.py -q -s
Remove-Item Env:EAH_RUN_REAL_QA
```

Docker 不在 PATH 时设置 `EAH_DOCKER` 为 CLI 完整路径。上述测试使用临时库、桶、独立队列与容器并清理，避免混入体验数据。`EAH_RUN_QUEUE` 默认 `eah.runs`，专用 Linux worker 并发 4，组织并发/排队额度仍由 PostgreSQL 裁决。迁移 `0007_runs` 只新增运行相关表与约束；降级仅用于空隔离库。详细证据见 [D08 记录](docs/D08-持久运行与文档问答实施记录.md)。

## 配置与排错

管理员进入知识库后展开“检索试查 · 管理员”，输入问题，选择全部就绪文档或当前页的一份文档，点击“开始试查”。需已保存可用的 Embedding 和 Rerank；每次查询会产生模型用量。结果显示原片段、文件与定位，可点击“查看文档记录”继续核对。空结果与模型故障分别显示；排序分数不是事实可信度，未展示片段不代表全文没有答案。查询过长时按提示缩短；“取消等待”停止本页等待，已发出的模型调用仍可能计费。

检索真实验收使用3份固定文档、2个中英文问题，独立数据库和桶自动清理，不修改体验资料：

```powershell
$env:EAH_TEST_ADMIN_URL='postgresql://eah:eah_dev@127.0.0.1:5432/postgres'
$env:EAH_RUN_REAL_RETRIEVAL='1'
uv run pytest tests/test_real_retrieval.py -q -s
Remove-Item Env:EAH_RUN_REAL_RETRIEVAL
```

该入口读取已有`.env`中的`EAH_PROBE_API_KEY`，显式运行会产生少量真实Embedding/Rerank调用。默认回归跳过，模型fixture结果不能替代此项；不包含答案生成或GitHub工具。详情见[D06记录](docs/D06-混合检索实施记录.md)。

`.env.example` 提供仅供本地开发的数据库连接。API/CLI 从根目录 `.env` 读取 `EAH_DATABASE_URL`；优先使用当前进程环境变量。`EAH_PROBE_*` 只供显式探针使用，不会在启动时调用外部账号；组织的模型连接通过设置页测试并保存到数据库。

身份配置：`EAH_BROWSER_ORIGINS` 为 JSON 数组，默认允许 `http://127.0.0.1:3300` 和 `http://localhost:3300`；自定义 Web 端口时必须同步修改。所有变更请求检查精确 Origin 和 CSRF。`EAH_COOKIE_SECURE=false` 仅用于本地 HTTP，部署 HTTPS 时设置为 `true` 并填写实际 HTTPS Origin。登录会话七天、匿名防伪会话三十分钟，Cookie 均为 HttpOnly、SameSite=Lax。HTTP API 客户端须先读 `/api/v1/auth/session` 保存两枚 Cookie，再将返回的 csrfToken 放入 `X-CSRF-Token`；只使用一枚 Cookie 或只传组织/角色不能登录。

| 情况 | 检查与处理 |
| --- | --- |
| Docker 安装提示缺少“虚拟机平台” | Windows 管理员终端执行 `wsl --install --no-distribution --web-download`，按系统提示重启后安装/启动 Docker Desktop；系统提权弹窗需本人处理 |
| Docker 命令存在但无法连接 | 先确认 `docker info` 成功，再运行 Compose；安装客户端不代表容器引擎已就绪 |
| `live` 200、`ready` 503 | 检查 `docker compose ps`，依次执行 Alembic 和 checkpoint 初始化；查看 API 日志 |
| 数据库端口占用 | 设置 `EAH_POSTGRES_PORT`，同时调整 `EAH_DATABASE_URL` 与 `EAH_TEST_ADMIN_URL` 的端口；不要停止其他项目进程 |
| API 8300 端口占用 | 设置 API 的 `EAH_API_PORT`；Web 启动前设置私有环境变量 `NUXT_API_BASE=http://127.0.0.1:新端口` |
| Windows psycopg 事件循环错误 | 使用上述 `python -m backend` 入口，已为 Python 3.12 选择 SelectorEventLoop；不要绕过入口直接用默认事件循环启动 uvicorn |
| `pnpm` 使用了不同 Node | 用 `pnpm exec node --version` 检查项目运行版本；`.node-version`、`devEngines.runtime` 和锁文件应一致 |
| 百炼测试返回 `MODEL_URL_DENIED`，DNS 指向 `198.18.*` | 检查本机代理的 fake-IP DNS；将 `dashscope.aliyuncs.com` 加入其域名排除列表并重载，确认解析为公共 IP。保留后端私网/保留地址检查 |

D03 部署配置：`EAH_MODEL_BASE_URLS` 为协议到 HTTPS Base URL 数组的 JSON 对象；默认允许 OpenAI `/v1`、Cohere `/v2`、百炼 `/compatible-mode/v1`（聊天/Embedding）及 `/api/v1`（Rerank）官方地址，精确匹配且不跟随重定向/环境代理。`EAH_CHAT_TOKENIZERS` 为聊天模型名到 tokenizer ID 的明确映射：OpenAI 使用 `o200k_base` 或 `cl100k_base`，Qwen 使用 `dashscope-qwen-1.26.7`；新增模型须先验证映射和能力，不能猜测。OpenAI 词表首次下载并缓存，Qwen 词表由固定版本官方 SDK 内置。`EAH_SYSTEM_LIMITS` 为七项硬上限对象，字段见 `.env.example`；由部署者修改并重启 API，浏览器只能设置组织上限。加载或能力测试失败不会启用配置。

## 工程验证

所有命令仍在根目录执行。无需真实模型账号的检查：

```powershell
pnpm lint
pnpm format:check
pnpm typecheck
pnpm test:unit
pnpm build
uv run ruff check backend scripts tests migrations
uv run ruff format --check backend scripts tests migrations
uv run mypy
uv run pytest -m "not integration"
uv run python -m scripts.export_openapi --check
```

接口变更后执行 `uv run python -m scripts.export_openapi` 和 `pnpm api:types`，同步生成的 OpenAPI 与客户端类型，再运行检查。

数据库和浏览器验证：

```powershell
# .env 中配置 EAH_TEST_ADMIN_URL；只连接本项目测试数据库管理员
uv run python -m scripts.probe_database

# 首次安装测试浏览器；保持 API/Web 已启动
pnpm --filter @eah/web exec playwright install chromium
pnpm test:e2e
```

D02 推荐使用隔离浏览器验收，自动创建临时库、迁移/checkpoint、临时 API 和生产 Web；执行结束停止测试进程并检查端口释放，删除本次临时库。不会写入正常体验库。需要先完成 `pnpm build`；以下为 PowerShell 命令：

```powershell
$eahNode = pnpm exec node -p process.execPath
uv run --locked python -m scripts.browser_smoke --node $eahNode
```

D03 页面保存流程使用显式受控提供方协议验收（真实 PostgreSQL/API/生产 Web，模型响应为 fixture）：`uv run --locked python -m scripts.browser_smoke --node $eahNode --model-fixture`。普通启动命令不会使用 fixture；此结果不能替代真实模型联调。

D04 集成需先启动本地 S3。以下测试创建唯一临时库、桶和队列，不写体验数据；浏览器脚本结束自动停止临时服务并检查端口释放：

```powershell
$env:EAH_TEST_ADMIN_URL='postgresql://eah:eah_dev@127.0.0.1:5432/postgres'
$env:EAH_TEST_S3_ENDPOINT='http://127.0.0.1:4566'
uv run --locked pytest -q
uv run --locked python -m scripts.browser_smoke --node $eahNode --model-fixture --storage
```

未设置 S3 测试地址时 D04 集成用例明确 skip，不能计为验收通过。主体验环境不会使用测试模型 fixture。

`pnpm test:e2e` 直接运行在 `EAH_WEB_URL`（默认 3300），会创建测试账号和组织；日常验收优先使用上述隔离入口。临时数据库连接仍来自 `EAH_TEST_ADMIN_URL`，不能指向生产管理员。

数据库探针新建唯一 `eah_d01_smoke_*` 临时库，验证空库升级、重复升级、向量近邻查询、checkpoint 初始化与新连接上的中断恢复，并在结束时删除本次临时库。不能对生产管理员连接运行。Pytest 集成入口为 `uv run pytest -m integration`，需提前设置进程变量 `EAH_TEST_ADMIN_URL`；未设置会明确 skip，不能计为集成通过。CLI 探针缺配置返回 2。

浏览器测试覆盖健康显示及注册、登录、刷新恢复会话、组织切换、账号改名/改密、成员权限和手机布局。健康页面测试允许“未就绪”分支；身份场景必须有真实已迁移数据库，不能用健康页面一项通过代替完整验收。

Agent入口为组织导航中的“Agent”（`/o/:orgId/agents`）。管理员可从空白或三模板创建停用配置，绑定知识库、只读工具与聊天模型，保存后根据缺失条件逐项完善并启用；成员只看启用项。每次保存追加内容版本，启停不增加内容版号，试运行按钮明确未开放。GitHub工具尚未接入，因此进展总结模板只能保存停用配置。

D07后端专项：设置上方测试数据库/S3变量后执行`uv run pytest tests/test_agents.py -q`。浏览器沿用隔离入口，设置`EAH_BROWSER_INGESTION=1`并传`--model-fixture --storage`，覆盖模板、脏表单、409保留/重载、复制、启停及成员直链。迁移`0006_agents`随`uv run alembic upgrade head`执行，降级仅在隔离验收库使用。配置保存与启停不调用模型，不产生模型用量。

```powershell
uv run python -m scripts.probe_external models
uv run python -m scripts.probe_external mcp
uv run --locked python -m scripts.probe_model_settings
```

最后一条逐类执行 D03 的同一能力探针：聊天/Embedding 使用 `EAH_PROBE_API_KEY`、`EAH_PROBE_BASE_URL` 及对应模型名；Rerank 使用 `EAH_PROBE_RERANK_API_KEY/BASE_URL/MODEL`。只有显式运行才调用真实服务，缺值返回 2 / UNVERIFIED；CLI 不绑定组织、不写应用用量表，API 测试则逐次持久记录用量。不要将提供方原始错误或 API Key 贴入报告。

百炼账号的本地 `.env` 可按下列映射填写；两个 API Key 字段填自己的凭据，不提交 `.env`。协议变量省略时沿用 OpenAI/Cohere 默认值。

```dotenv
EAH_PROBE_CHAT_PROTOCOL=dashscope-chat
EAH_PROBE_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
EAH_PROBE_CHAT_MODEL=qwen-plus
EAH_PROBE_EMBEDDING_MODEL=text-embedding-v4
EAH_PROBE_RERANK_PROTOCOL=dashscope-rerank
EAH_PROBE_RERANK_BASE_URL=https://dashscope.aliyuncs.com/api/v1
EAH_PROBE_RERANK_MODEL=gte-rerank-v2
```

真实 API 保存、用量落库和上传验收需要本地 PostgreSQL/S3 与上面的百炼 Key。下面的显式测试会产生少量模型调用，创建并清理临时数据库/桶，不修改体验组织；日常 pytest 默认跳过此项：

```powershell
$env:EAH_TEST_ADMIN_URL='postgresql://eah:eah_dev@127.0.0.1:5432/postgres'
$env:EAH_RUN_REAL_BAILIAN='1'
uv run --locked pytest tests/test_real_bailian.py -q
Remove-Item Env:EAH_RUN_REAL_BAILIAN
```

该测试证明能力测试、配置保存、上传接受及版本绑定；文件仍为 queued，不证明 D05 的解析与向量生成。

根据 `.env.example` 配置本任务专用服务后才执行真实外部探针：模型项发送一次限额聊天请求和一次 Embedding 请求；MCP 项只对固定 GitHub 服务执行 initialize/list_tools。退出码 `0=PASS`、`1=FAIL`、`2=UNVERIFIED（缺配置）`。本地协议 fixture 不验证真实提供方、仓库授权或完整工具业务。

构建产物位于 `apps/web/.output`；可用 `pnpm --filter @eah/web preview --port 3300` 验证构建版本。Python 源码与共享后端包由 uv 管理；完整部署与分发验收属于 D20。

## 产品定位

用户上传产品/技术文档并建立知识库，配置 Agent，结合 GitHub Issue/PR 的只读查询，完成文档问答、需求与方案对比、进展与风险总结，获得带引用来源的结果及可查看的执行轨迹。

首版已确认工作台、知识库、Agent 管理、对话与任务、连接与工具、执行记录与用量、团队与设置七个模块。具体功能、交互流程和验收标准以 [PRD](docs/PRD.md) 为产品基线。

## 目标演示流程

上传文档 → 解析、切片和向量化 → 提出跨文档问题 → Agent 规划与检索 → 调用工具 → 返回答案、引用来源和执行轨迹。

## 第一版范围

- 用户登录、组织与成员管理，后端落实组织数据隔离。
- 知识库管理，支持 PDF、TXT、MD 文档上传、解析和切片预览。
- Embedding、pgvector、混合检索及 Rerank，回答可追溯到来源。
- Agent 配置、任务规划、执行循环、Function Calling，并通过真实 GitHub MCP 工具只读查询允许仓库的 Issue/PR。
- 对话与流式响应、会话历史恢复、执行轨迹、Token 与耗时统计。
- 执行步数、运行时间、工具超时、重试次数和 Token 预算上限，以及用户终止机制。

多 Agent 协作、长期记忆增强、上下文压缩、评测管理界面、工作流编辑器、多模型路由和人工审批留待后续迭代；小型固定评测集属于首版工程验收。

## 技术栈

已初始化依赖的精确版本以 pnpm/uv 锁文件及 [D01 版本证据表](docs/D01-工程初始化验证.md) 为准。Tailwind、shadcn-vue、S3 和业务 worker 在对应任务实际使用时接入，不预建空模块。

| 层级 | 规划技术 |
| --- | --- |
| 前端 | Nuxt 4、Vue 3、TypeScript |
| 界面 | Tailwind CSS、shadcn-vue |
| 后端与 Agent | Python、FastAPI、LangGraph |
| 模型与检索服务 | OpenAI-compatible API、Embedding API、Reranker |
| 数据与任务 | PostgreSQL、pgvector、Redis、Celery |
| 存储与部署 | S3 兼容存储、Docker Compose |
| 测试 | Pytest、Playwright |

## 开发顺序与验收

1. 明确 PRD、架构和数据模型，搭建前后端及基础服务。
2. 完成登录、组织隔离和知识库管理，验证跨组织访问被拒绝。
3. 打通文档入库与 RAG，验证回答引用可追溯且检索遵守组织权限。
4. 完成 Agent 执行、工具调用与 MCP，验证正常完成、超时、失败和主动终止。
5. 完成对话、会话恢复及执行轨迹，跑通跨文档问答演示。
6. 补齐测试、Docker 启动说明、架构说明和演示材料。

实施工时与依赖以 [D01～D20 开发任务](docs/development-plan.md) 为准；D01～D08 已实现，对应验收证据见各阶段记录。下一项为 D09，尚未实施；外部 S3 与 GitHub MCP 仍待对应环境和账号验收。

## 产品文档

产品范围见 [PRD](docs/PRD.md)，实现设计见 [技术方案](docs/technical-design.md)，迭代范围与验收映射见 [开发任务清单](docs/development-plan.md)。
