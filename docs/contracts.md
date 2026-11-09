# 接口与事件契约

本文是第一版实现基线，集中定义跨前端、API、worker 与 Agent 的可观察行为。D01 健康端点、D02 AP01～AP15、D03 AP16～AP20、D04 AP21～27/AP29/AP30 original/AP61 document.upload 已实现；D05 AP28/AP30 text/AP31、D06 AP32检索试查已实现；其余能力待后续任务。已实现接口具有 Pydantic Schema、契约测试、OpenAPI 和生成的客户端类型，证据见 [D02 记录](D02-身份组织实施记录.md)、[D03 记录](D03-模型配置实施记录.md)、[D04 记录](D04-知识库实施记录.md)。百炼真实模型及真实配置下的上传见 [补充验收](D03-D04-百炼补充验收.md)，外部 S3 尚未验证。文档处理证据见 [D05 记录](D05-文档入库实施记录.md)，检索证据见 [D06 记录](D06-混合检索实施记录.md)。数据持久化见 [数据模型](data-model.md)，流程见 [Agent 执行设计](agent-flow.md)。

## HTTP 通用约定

- 基础路径 `/api/v1`；JSON 字段 camelCase，Python/数据库内部可用 snake_case，通过显式 DTO 映射。
- ID 为 UUID 字符串；时间为 UTC ISO 8601；分页使用稳定排序与不透明 cursor，默认 limit=20，最大 100。过滤与排序字段采用允许列表。
- 成功响应 `{ data, requestId }`；列表为 `{ data: { items, nextCursor }, requestId }`。204 没有响应体，SSE/文件下载不套 JSON 外壳。
- 错误响应 `{ error: { code, message, details? }, requestId }`；details 仅含允许公开的字段错误，不返回堆栈、凭据或原始提供方响应。
- 200 查询/更新、201 创建、202 已接受异步任务、204 删除成功；401 未认证、403 同组织已知资源的权限不足、404 不存在或不可见资源、409 幂等内容冲突/版本冲突/会话繁忙、413 超限、415 类型不支持、422 字段无效、429 限流、503 暂时不可用。
- `Idempotency-Key` 用于文档任务和 run 创建，作用域是组织、发起者、操作与资源；相同键不同请求摘要返回 409，记录与目标业务实体共同保留到实体被清理。

## 身份与角色

身份从服务端会话取得；组织上下文可由客户端选择，但必须服务端校验成员关系。第一版角色为 owner、admin、member：owner 管理组织所有权及管理员；admin 管理成员、知识库、Agent 与工具配置；member 使用组织内已授权知识库和 Agent，并查看自己的会话/运行。admin/owner 可审计本组织运行。角色变更、订阅和工具执行均适用同一授权规则。

## 核心操作

| 操作 | 拟定端点 | 输入和返回边界 |
| --- | --- | --- |
| 文档上传 | `POST /knowledge-bases/{id}/documents` | multipart 文件 + 幂等键；202 返回 Document（id、status 等） |
| 文档状态 | `GET /documents/{id}` | 返回解析状态、失败码、版本；不直接暴露对象存储凭据 |
| 启动运行 | `POST /conversations/{id}/runs` | agentId、input、幂等键；202 返回 runId、status、eventsUrl |
| 运行快照 | `GET /runs/{id}` | status、cancelRequested、lastSeq、结果/引用或 errorCode |
| 事件订阅 | `GET /runs/{id}/events` | 同源会话鉴权；支持 Last-Event-ID，流协议见下文 |
| 请求取消 | `POST /runs/{id}/cancel` | 幂等；202 表示已记录请求，200 表示已是终态；返回当前快照 |

## 状态与初始限额

文档状态：`queued → processing → ready | failed`；重试仅允许 `failed → queued` 并创建新处理 attempt。删除后立即对检索不可见，不把删除过程伪装为 ready。

运行状态：`queued → running → succeeded | failed | cancelled | timed_out`；排队期可直接 cancelled，排队过期直接 failed（RUN_QUEUE_TIMEOUT）；恢复已开始运行仍为 running，终态不可退回。cancelRequested 是独立标记，不等同 cancelled。失败码至少区分 TOOL_DENIED、TOOL_FAILED、TOOL_OUTCOME_UNKNOWN、MODEL_UNAVAILABLE、BUDGET_EXCEEDED、RUN_INTERRUPTED。

| 项目 | MVP 初始约束 |
| --- | --- |
| 文档 | PDF、TXT、MD；单文件最多 20 MiB；解析失败显式反馈 |
| 单次 run | 最多 10 次模型决策步骤、10 次工具调用尝试（含重试） |
| 时间 | 执行最长 120 秒（从首次 running 起，恢复不重置）；单模型/工具调用最长 30/15 秒，均受剩余总时限约束 |
| 重试 | 同一可重试操作最多额外 2 次；仍受步骤、工具次数和总时限限制 |
| Token | run 聊天模型总输入与输出预算初始 16000；每次调用预留最大输出并根据实际 usage 扣减，usage 缺失按预留保守计量并标记估算；其他服务用量分开记录，见下文 |
| 并发 | 同一会话最多一个非终态 run；跨会话限额在部署配置中明确 |

这些数字是待实现的产品初始限制，不是已测性能或 SLA；修改时同步运行配置、前端提示与边界测试。Token 口径及部署默认值见下文，组织只能降低可配置上限。

## SSE 与恢复

持久化事件包含 `runId、seq、type、occurredAt、data`；seq 在 run 内严格递增、唯一，SSE id 使用 `${runId}:${seq}`。事件类型为 run.started、message.delta、tool.started、tool.completed、tool.failed、run.completed、run.failed、run.cancelled、run.timed_out。只发送面向用户的有限信息，隐藏内部异常与敏感工具内容。

先持久化事件再发布；模型文本增量可合并后持久化。客户端按 seq 去重，只推进连续序号，发现缺口先补拉，禁止仅记录最大 seq 而跳过未到事件。断线依据 Last-Event-ID 补发；游标越界/已清理返回明确恢复错误，客户端重新查询快照和消息，不重建 run。订阅权限失效时关闭流。

终态以数据库条件更新保证唯一，事件可重复投递但不能重复改变状态。测试必须覆盖“终态已提交但通知丢失”和“取消与成功同时到达”。最终回答仅在校验完成、内容与引用落库后发 run.completed。

## 请求身份与字段规则

### D01 基础健康端点

健康端点匿名读取，不要求组织选择器或会话，不提供数据库地址和底层异常。

| 路由 | 状态与响应 | 验证范围 |
| --- | --- | --- |
| `GET /api/v1/health/live` | 200，`{data:{status:"ok",service,version},requestId}` | API 进程存活，不查询数据库 |
| `GET /api/v1/health/ready` | 200，data.status 为 `ready`；未就绪时 503，`{error:{code:"SERVICE_NOT_READY",message},requestId}` | 数据库可连接、Alembic 版本到 head、app/checkpoints schema、vector 扩展、checkpoint 四张表及适配器迁移版本就绪；最长等待 5 秒 |

两者均返回服务端生成的 `X-Request-Id`，与正文 requestId 一致。Redis 和真实模型/MCP 连通性由独立探针检查，不由上述接口推断。后端 Schema 生成 [当前已实现 OpenAPI](openapi.json) 及前端类型；接口实现范围以本文首段及 OpenAPI 为准。

### 业务请求约束

- 除身份接口和组织列表/创建外，请求携带 `X-Organization-Id`。它只是选择器，服务端必须从 Cookie 会话验证当前成员关系；正文不得传入可信的 userId/role。日志中的 requestId 由服务端生成。
- 使用同源 `/api/v1` 反向代理；会话为数据库保存哈希的随机 opaque token，浏览器 Cookie 为 HttpOnly、Secure（本地 HTTP 开发例外）、SameSite=Lax。`GET /auth/session` 匿名也返回 200，包含 user=null 和 csrfToken；服务端签发绑定同源 Cookie 的 CSRF token。所有变更请求检查 Origin 与 `X-CSRF-Token`，登录后轮换会话和 CSRF token。
- JSON 未知字段拒绝；名称 trim 后 1～100 字符，说明最多 1000 字符，提示词最多 12000 字符，问题/关注点最多 4000 字符，标题最多 100 字符。文本不因含中文被错误按字节截断。密码保持 PRD 的 8～128 字符，不 trim。
- 编辑请求携带 `expectedRevision`，不一致返回 `VERSION_CONFLICT` 和当前 revision，不覆盖别人修改。创建、文件重试、run、所有权转移使用幂等键；取消、删除、启停按目标状态幂等。重放成功响应仍先检查当前访问权限。
- 首次 PUT 尚不存在的模型/GitHub 配置时 expectedRevision=0，已有配置必须使用返回的 revision；同组织当前配置指针与模型/连接版本在事务内原子更新。
- 列表默认 `createdAt desc,id desc`；会话为 `updatedAt desc,id desc`。cursor 绑定组织、用户权限范围、过滤条件和排序，不能换条件复用。使用 last value 游标，不承诺列表在并发变更时具有历史快照。
- 时间筛选传 `startDate/endDate`（YYYY-MM-DD）和 IANA `timezone`；后端转换为 `[起日当地 00:00,止日次日当地 00:00)` UTC 区间。跨夏令时按当地日历运算，不固定加 24 小时。T3 止日不得超过该时区今日。
- 所有返回的权限提示是 UI 辅助，不能代替后端鉴权。每个业务 ID 都按 `(organizationId,id)` 查找；外组织 ID 一律 404，不通过错误内容泄露名称。

## API 清单

表中地址均省略 `/api/v1`。`U` 表示登录账号；`M` 表示当前组织成员；`A` 表示 owner/admin；`O` 表示 owner；`本人` 表示资源创建者。管理员审计权只读，不包含他人会话编辑、追问、取消。资源类型与字段见下一节。

### 身份、组织和设置

D02 会话使用 `eah_session` 与 `eah_csrf` 两枚 host-only HttpOnly Cookie，数据库只存哈希。匿名会话三十分钟，登录会话七天；注册成功自动登录，注册/登录均撤销旧会话并轮换 Cookie。API 与含身份的 SSR 页面均 `Cache-Control: no-store`。AP15 的 expectedRevision 对应组织 accessRevision；所有权转移成功后原 owner 已无转移权限，旧请求重放返回 403，须重新读取 AP09 确认结果。退出请求携带已撤销会话原有 CSRF 重试仍可清 Cookie，不恢复认证。

| ID | 方法与地址 | 权限、输入 → 输出 / 关键错误 |
| --- | --- | --- |
| AP01 | `GET /auth/session` | 匿名/U；返回 user、csrfToken、organizations 简表、当前会话 expiresAt；匿名 organizations=[] |
| AP02 | `POST /auth/register` | 匿名；email、password、displayName → 201 User；邮箱规范化后唯一，重复 EMAIL_EXISTS；注册不冒充邮箱已验证 |
| AP03 | `POST /auth/login` | 匿名；email、password → 200 User 并设置轮换 Cookie；失败统一 AUTH_INVALID |
| AP04 | `POST /auth/logout` | U；撤销当前会话并清 Cookie → 204；已注销仍可清 Cookie |
| AP05 | `PATCH /me` | U；displayName、expectedRevision → User |
| AP06 | `POST /me/password` | U；currentPassword、newPassword → 204 并撤销该账号全部会话；失败 CURRENT_PASSWORD_INVALID |
| AP07 | `GET /organizations` | U；返回当前有成员关系的 Organization 列表 |
| AP08 | `POST /organizations` | U；name、幂等键 → 201 Organization；组织、创建者 member 记录和 owner 指针同事务建立 |
| AP09 | `GET /organizations/current` | M；Organization、effectiveRole、accessRevision、settingsRevision；供页面恢复权限与组织切换 |
| AP10 | `GET /members` | M；分页 Member 列表，包含 displayName、email、effectiveRole、joinedAt |
| AP11 | `POST /members/resolve` | A；email → 已注册账号最小身份供确认，未注册 ACCOUNT_NOT_FOUND；不提供公共账号搜索 |
| AP12 | `POST /members` | A；email、幂等键 → 201 Member，固定 member；已加入 200 同一记录 |
| AP13 | `PATCH /members/{userId}` | O；baseRole=admin/member、expectedRevision → Member；当前 owner 不可在此修改 |
| AP14 | `DELETE /members/{userId}` | O 可移除非 owner；admin 仅移除 member → 204；组织锁内更新 accessRevision |
| AP15 | `POST /organizations/current/transfer-owner` | O；targetUserId、expectedRevision、幂等键 → Organization；目标必须仍在组织；原 owner 转 admin |
| AP16 | `GET /settings/models` | M；成员只得可用模型 id/name/kind；A 额外得到连接状态、baseUrl、modelName、dimension、hasCredential、revision，不得得到凭据 |
| AP17 | `PUT /settings/models/{kind}` | A；ModelConfigWrite → ModelConfig；kind=chat/embedding/rerank，每类一个连接；保存前服务端测试，失败不替换生效配置 |
| AP18 | `POST /settings/models/{kind}/test` | A；候选 ModelConfigWrite 或已保存 connectionId → TestResult；明确可能产生用量，不生成会话 |
| AP19 | `GET /settings/limits` | M；系统硬上限、组织生效上限、可编辑范围、revision |
| AP20 | `PUT /settings/limits` | A；允许降低的 limits、expectedRevision → 生效上限；超系统硬值 LIMIT_INVALID；修改仅影响新接受的任务 |

D03 补充契约：AP16 的 data 为 `{items,options}`，成员的 items 每项严格只有 id/name/kind，options 为空；管理员 options 提供部署允许的协议/地址与受支持聊天模型，配置项补充 tokenizerId、capabilities、embeddingLocked。同一 kind 可有多个协议选项，聊天模型列表按协议过滤。协议固定为 `openai-chat`、`dashscope-chat`、`openai-embeddings`、`cohere-rerank-v2`、`dashscope-rerank`；地址仅 HTTPS 精确匹配、禁止重定向和私网/保留地址目标；不是任意代理。

AP17 每次服务端重测，失败返回 422 和稳定错误码，不改变旧连接或 revision。AP18 候选测试返回 TestResult（失败 ok=false），已保存连接测试只接受 `{connectionId}`；两者都不创建聊天会话。更换地址/协议不能省略 credential，null/空字符串拒绝；其余保存可省略并保留原凭据。网络调用前后都校验当前会话、成员、角色及 revision，调用期间不占组织/账号锁。测试使用固定短输入，30 秒总时限，聊天两次、Embedding/Rerank 一次；不自动重试。返回 usage 为各尝试数组，tokenSource 区分 actual/reserved/unavailable，billedUnits 独立保留重排计量，绝不混成聊天 Token。

AP19 返回 `{system,effective,editable,revision}`；七个可调字段见下方部署表，初始未保存 revision=0。AP20 接受 `{limits,expectedRevision}`；字段必须为正整数且不高于系统硬值，未知字段/浮点/布尔拒绝。组织可把先前降低的数值恢复到硬值以内，不限制成只能逐次递减。部署硬值降低后读取以新硬值截顶；正在运行任务快照由后续 run 接受流程负责。

### 知识库和 Agent

| ID | 方法与地址 | 权限、输入 → 输出 / 关键错误 |
| --- | --- | --- |
| AP21 | `GET /knowledge-bases` | M；q、cursor → KnowledgeBase 列表与状态计数 |
| AP22 | `POST /knowledge-bases` | A；name、description、幂等键 → 201 KnowledgeBase；同组织有效名称重复 NAME_EXISTS |
| AP23 | `PATCH /knowledge-bases/{id}` | A；name、description、expectedRevision → KnowledgeBase |
| AP24 | `DELETE /knowledge-bases/{id}` | A；无 Agent 当前版本绑定时逻辑删除 → 204；有绑定 KB_IN_USE，details 给本组织 Agent id/name；旧版本历史引用不阻止删除 |
| AP25 | `GET /knowledge-bases/{id}/documents` | M；q、status、cursor → Document 列表，同名不去重 |
| AP26 | `POST /knowledge-bases/{id}/documents` | A；multipart file、Idempotency-Key → 202 Document；服务端流式检查实际大小、格式、空内容；只在记录和 outbox 已提交后接受 |
| AP27 | `GET /documents/{id}` | M；Document、当前 attempt 和历史尝试摘要、cleanupStatus；权限允许时才可见 |
| AP28 | `POST /documents/{id}/retry` | A；expectedAttemptId、幂等键 → 202 Document；只有 failed 可重试，旧 attempt 已变则 DOCUMENT_STATE_CONFLICT |
| AP29 | `DELETE /documents/{id}` | A；逻辑删除、递增 accessRevision、登记清理任务 → 204；检索/正文立即不可访问 |
| AP30 | `GET /documents/{id}/content` | M；view=text/original、attemptId；text 只接受该文档已发布 ready 版本并返回有界分页 TextPage，原文件经 API 代理下载；不发长期对象存储直链 |
| AP31 | `GET /documents/{id}/chunks` | A；attemptId、cursor → ChunkPreview；普通来源抽屉通过 AP52 读自己已用来源，无需开放全量调试片段 |
| AP32 | `POST /knowledge-bases/{id}/search-preview` | A；query、documentIds? → RetrievalResult；仅当前库，产生检索用量、不创建会话或 run |
| AP33 | `GET /agents` | M；taskType、q、cursor → AgentSummary；A 可筛 enabled=false，成员只见启用项 |
| AP34 | `GET /agents/{id}` | M 读可用摘要；A 读完整 AgentConfig、当前版本、revision；已停用成员访问返回不可用 |
| AP35 | `POST /agents` | A；AgentWrite、幂等键 → 201 AgentConfig；可保存停用且未完成配置；创建时固定 taskType |
| AP36 | `PUT /agents/{id}` | A；完整 AgentWrite、expectedRevision → 新版本；不得改变 taskType；enabled=true 时必须通过 readiness 校验 |
| AP37 | `POST /agents/{id}/clone` | A；newName、幂等键 → 201 新停用 Agent；只复制仍有效的本组织资源引用，不复制凭据 |
| AP38 | `PATCH /agents/{id}/status` | A；enabled、expectedRevision → AgentSummary；启用检查条件，停用更新 accessRevision；旧配置版本保留 |

D07 已实现 AP33～AP38，新增管理员 `GET /agents/options` 返回三模板与固定工具目录（`templates`、`tools`），只提供配置定义，不执行工具。AP33支持limit=1～100、q最多200字符，按createdAt/id分页。成员强制只见启用项，AP34只返回AgentSummary；停用直链404 `AGENT_UNAVAILABLE`，跨组织404。管理员可以读取完整配置；所有写入口检查Origin/CSRF和角色。

AgentWrite名称1～100字符、说明最多2000、提示词最多20000、知识库最多100且不重复、工具最多5且不重复；仅接收固定工具key，不接受凭据或自定义执行代码。停用草稿允许空提示词/知识库/模型，但显式资源引用仍须属于当前组织且可见。启用要求Chat/Embedding/Rerank启用、当前Embedding profile下至少一份ready且有片段的资料、任务必需工具；缺项409 `AGENT_NOT_READY`，details.readiness包含code/message/field/action。配置检查不调用提供方，不能保证后续外部调用成功。D07的GitHub工具标为不可用，progress只能保存停用草稿，D13接入后再补允许仓库检查。

创建/复制要求幂等键，同键同内容不重复建Agent，同键异内容409；重放返回原目标的当前配置。编辑/启停expectedRevision冲突409 `VERSION_CONFLICT`，修改任务类型409 `TASK_TYPE_IMMUTABLE`。每次PUT追加不可变内容版本；启停只改资格及revision，不增加内容版号。所有配置/启停变更递增accessRevision。复制强制新名称和停用状态，剔除不可用资源后在removedReferences说明；不复制模型凭据。D07不开放会话/run或真正试运行接口。

D05 已实现 AP28、AP30 text、AP31：仅当前 ready attempt 可预览，非就绪 `DOCUMENT_NOT_READY`、旧版本 `DOCUMENT_STATE_CONFLICT`；text 每页4000字符，chunks 每页20项。cursor 是此不可变版本内的偏移标识，客户端仅回传 nextCursor；非法位置 `CURSOR_INVALID`。ChunkPreview 为 `{documentId,attemptId,items:[{chunkId,ordinal,text,locator}],nextCursor}`，TextPage 为 `{documentId,attemptId,text,nextCursor}`，均套标准 Envelope。PDF 错误为 `PDF_INVALID/PDF_ENCRYPTED/DOCUMENT_NO_TEXT`；处理租约过期 `INGEST_INTERRUPTED`，维度不符 `EMBEDDING_DIMENSION_INVALID`。重试复用已锁定 profile、快照当前同语义凭据；同键重放返回原接受快照，实际状态通过 AP27 获取。

D06 AP32 已实现，管理员入口验证 Origin/CSRF。`query` trim后1～4000字符；提供方保守上界4000 UTF-8字节，超出返回422 `QUERY_TOO_LARGE`，不截断原问题。`documentIds`省略或空数组表示当前库全部就绪文档，最多1000个且不重复；显式越界/删除选择器404，未就绪409 `DOCUMENT_NOT_READY`。无就绪文档200空结果且无模型调用；缺模型409 `MODEL_NOT_CONFIGURED`。检索总时限15秒，共享于Embedding/SQL/Rerank；超时503 `RETRIEVAL_TIMEOUT`，提供方失败503及稳定`MODEL_*`码，不返回假空结果、不自动重试。

调用期间不持组织锁，调用前和交付前重新授权并检查当前文档attempt/模型版本；资源删除或撤权403/404，版本改变409 `RETRIEVAL_SCOPE_CHANGED`/`MODEL_CONFIG_CHANGED`。用量以operationId关联、operationKind=search_preview，不建run。`usage`沿用TestUsage，attempt=1查询Embedding、2重排；Rerank billedUnits独立计量。

`coverage`包含eligibleDocumentCount/eligibleChunkCount、vectorCount/keywordCount/candidateCount/returnedCount、omittedChunkCount/omittedDocumentCount、excerptTruncatedCount/excerptTruncatedDocumentCount/contextTruncatedCount、queryTermsTruncated、evidenceBytes、budgetBasis、partial、note。词元最多64，重排连续摘录最多160 UTF-8字节，证据文本最多4000 UTF-8字节，budgetBasis=utf8-byte-upper-bound（保守token上界而非实际用量）。searchedDocumentIds只含实际有有效片段的文档。partial为false也只说明没有数量/摘录裁剪，不表示已理解全文或答案完备。现有D05每片最多384字节，8片不超过3072字节；4000字节正文兜底不改变当前切片策略。

### GitHub、工作台、会话和运行

| ID | 方法与地址 | 权限、输入 → 输出 / 关键错误 |
| --- | --- | --- |
| AP39 | `GET /connections/github` | M 返回可用性及允许仓库摘要；A 加连接名称、状态、hasCredential、lastTestedAt、revision |
| AP40 | `PUT /connections/github` | A；name、credential?、enabled、expectedRevision → GitHubConnection；提供方/服务地址固定；首次必填凭据，更换时先测试；关闭无需外部测试 |
| AP41 | `POST /connections/github/test` | A；候选 credential 或使用已保存连接 → TestResult；失败不覆盖当前有效凭据 |
| AP42 | `GET /connections/github/repositories` | M；可用 Repo 列表；A 可看失效状态 |
| AP43 | `POST /connections/github/repositories` | A；owner、repo、shareWithOrganization=true、幂等键 → 201 Repo；MCP 验证可读并保存外部稳定 ID，不能仅凭搜索零条判定可读 |
| AP44 | `DELETE /connections/github/repositories/{id}` | A；移除允许范围、递增 accessRevision → 204；保留历史关联，不物理删除快照 |
| AP45 | `GET /tools` | M；固定 ToolSummary 清单：key/name/description/readOnly/enabled/inputSummary；不返回原始外部工具 Schema 或敏感参数 |
| AP46 | `PATCH /tools/{key}` | A；enabled、expectedRevision → ToolSummary；只能修改内置注册项，停用影响授权；知识检索为必需工具，不可关闭 |
| AP47 | `GET /dashboard` | M；三模板 readiness、自己的最近会话及当前组织概览；ready 不代表外部调用一定成功 |
| AP48 | `GET /conversations` | 本人；q、cursor → ConversationSummary，按更新时间排序 |
| AP49 | `POST /conversations` | 本人；agentId、taskType、title?、幂等键 → 201 Conversation；类型必须与 Agent 一致，不启动模型 |
| AP50 | `GET /conversations/{id}`、`PATCH /conversations/{id}` | 本人；GET 返回固定身份和 activeRun；PATCH 只可传 title、expectedRevision；审计通过 run 详情，不开放他人会话写入口 |
| AP51 | `GET /conversations/{id}/messages` | 本人；cursor → Message 分页，含每轮 runId、来源可见性、结果正文；应用撤销规则 |
| AP52 | `GET /runs/{id}/sources/{sourceId}` | 本人或 A 审计；SourceDetail；删除仅返回 tombstone，撤销返回 SOURCE_REVOKED；请求必须匹配本 run 的来源 |
| AP53 | `POST /conversations/{id}/runs` | 本人；RunCreate、幂等键 → 202 RunAccepted；同会话已有非终态返回 CONVERSATION_BUSY；先落库再投递 |
| AP54 | `GET /runs/{id}` | 本人或 A 审计；RunSnapshot；当前权限决定 resultVisibility，不因历史 snapshot 绕过 |
| AP55 | `GET /runs/{id}/events` | 本人或 A 审计；fetch SSE、Last-Event-ID → 持久事件；逐批校验权限，管理员不能据此取消 |
| AP56 | `POST /runs/{id}/cancel` | 仅本人；取消请求 → 202/200 RunSnapshot，幂等且不重置预算 |
| AP57 | `GET /runs` | M 默认 scope=mine；A 可 scope=organization；status、agentId、起止日期/时区、cursor → RunSummary 列表 |
| AP58 | `GET /runs/{id}/steps` | 本人或 A 审计；cursor → StepSummary；无隐藏思维链、原始凭据或已撤销来源正文 |
| AP59 | `GET /usage/summary` | 同 AP57 作用域与过滤条件 → UsageSummary；按 run.createdAt 筛选，不按完成时间偷偷换口径 |
| AP60 | `GET /runs/{id}/export` | 本人或 A 审计；format=markdown；仅 succeeded 且可见时下载，失效引用输出删除标记；撤销混合正文拒绝导出 |
| AP61 | `GET /idempotency-requests/{key}` | 原发起者；operation、resourceId? → accepted/absent 及 targetId/当前快照；接受结果未知时使用；absent 不保证正在传输的原请求不会随后提交 |

AP61 返回 absent 后如仍需提交，必须使用原键和相同内容，不能换新键；已在服务端接受的上传，客户端仅 AbortController 中断传输不等于删除记录。上传摘要由实际文件 hash、目标库和规范化元数据组成，服务端保存完整 hash 后才能确认重放相同内容。

D04 实现细节：AP26 每次只收一个 `file` part，最多 180 秒，扩展名/MIME/真实内容与实际字节数均受限；空 MIME/octet-stream 仅在扩展名受支持且内容校验通过时兼容。AP26 返回 202 Document；AP27 返回 `{document, attempts}`，管理员可见最近 20 次尝试。AP30 original 返回 attachment 二进制并在存储读取后再次鉴权，D05 text 返回当前已就绪文本，未就绪返回 409 DOCUMENT_NOT_READY。删除独立设置 deletedAt，普通列表/详情/下载返回不可见，原发起者 AP61 仍可核对已接受且已撤回的记录。

详情补充接口：`GET /knowledge-bases/{id}` 返回 KnowledgeBase；`GET /knowledge-bases/{id}/upload-policy` 返回 `{maxBytes,extensions,embeddingReady,maxDocuments}`，均要求当前组织成员身份。AP61 当前只接受 `operation=document.upload` 且 resourceId 为必填知识库 ID；其他操作随对应任务扩展，不返回跨发起者记录。拒绝码包括 FILE_EMPTY、FILE_TOO_LARGE、FILE_TYPE_UNSUPPORTED、UPLOAD_INCOMPLETE、IDEMPOTENCY_CONFLICT、EMBEDDING_NOT_CONFIGURED、DOCUMENT_LIMIT_REACHED；错误仍使用通用 JSON 外壳。

## 请求与响应 DTO

下表的问号表示可选；所有未标可选字段必填。业务 DTO 不直接复用 ORM 模型。写入凭据字段只进请求，不进入任意 Response Schema；空字符串不表示“保持”，字段省略才表示保留当前值。

| DTO | 字段与约束 |
| --- | --- |
| User / Organization / Member | User=id,email,displayName,revision；Organization=id,name,effectiveRole,accessRevision,settingsRevision,createdAt；Member=userId,displayName,email,effectiveRole,joinedAt,revision |
| TestResult | ok、testedAt、provider、modelName?、latencyMs?、usage?、errorCode?、message；usage 缺失标不可得，连接测试结果不充当永久授权 |
| ModelConfigWrite | providerProtocol、baseUrl、credential?、modelName、embeddingDimension?、expectedRevision；kind 来自路径；URL 只能来自部署允许的提供方地址，非任意网络代理；维度由真实测试验证 |
| ModelConfig | id、kind、name、providerProtocol、baseUrl、modelName、dimension?、enabled、hasCredential、lastTestedAt、testStatus、revision、profileId；chat 修改发布新 profile；embedding 有存量文档/处理中 attempt 时拒绝改变协议/模型/维度，维护重建不做首版 UI |
| KnowledgeBase | id、name、description、documentCount、readyCount、processingCount、failedCount、revision、createdAt、updatedAt |
| Document | id、knowledgeBaseId、fileName、mimeType、sizeBytes、status、currentAttemptId、attemptNumber、chunkCount、errorCode?、errorMessage?、createdAt、readyAt?、deletedAt?；cleanupStatus 仅管理视图 |
| TextPage / ChunkPreview | documentId、attemptId、text/items、nextCursor；片段含 chunkId、ordinal、text、locator；locator 为 pdf(pageStart,pageEnd) 或 text(headingPath,paragraphStart,paragraphEnd)，实际有才返回 |
| RetrievalResult | operationId、query、searchedDocumentIds、items[{chunkId,documentId,attemptId,fileName,text,locator,score}]、coverage、usage；score 仅排序含义，不显示成“事实可信度” |
| AgentWrite | name、description、taskType=qa/compare/progress、prompt、knowledgeBaseIds[]、toolKeys[]、chatConnectionId?、enabled；绑定逻辑聊天连接，新 run 固化其当前 profile/连接版本；停用时允许缺模型/空库；启用要求模型可用、至少一份就绪文档，progress 额外要求 GitHub 工具与允许仓库 |
| AgentSummary | id、name、description、taskType、enabled、currentVersionId、versionNumber、revision、readiness[{code,message,field,action}]、createdAt、updatedAt；无Prompt、资源绑定、模型连接或凭据 |
| AgentConfig | AgentSummary + prompt、knowledgeBaseIds、toolKeys、chatConnectionId、removedReferences[]；工具定义、凭据、执行上限不接受 prompt 覆写 |
| Repo | id、githubRepositoryId、owner、name、fullName、htmlUrl、enabled、lastVerifiedAt；运行只接受本平台 id，不接受模型提供 repo URL |
| ConversationSummary | id、title、agentId、agentName、taskType、updatedAt、activeRunId?、lastRunStatus?；组织和 owner 从鉴权确定 |
| RunCreate | agentId、input、retryOfRunId?；agentId 必须等于会话固定 Agent；retryOf 必须属于本会话且为失败/取消/超时终态；新运行用当前已保存配置 |
| RunAccepted | runId、conversationId、status、eventsUrl、createdAt；返回前用户消息、run、幂等记录、outbox 已同事务提交 |
| RunSnapshot | id、conversationId、taskType、status、cancelRequested、inputSummary、configVersion、budgetSnapshot、createdAt、startedAt?、finishedAt?、lastSeq、partialMessage?、result?、citations[]、coverage、resultVisibility、errorCode?、retryOfRunId? |
| Message | id、runId、role=user/assistant、content、isPartial、resultVisibility、citations[]、createdAt；结果结构化数据在 result，Markdown 由平台受控渲染 |
| SourceDetail | id、kind=document/github、availability=available/deleted/revoked/unavailable、title、excerpt?、locator?、documentId?、attemptId?、chunkId?、repoId?、itemType?、number?、htmlUrl?、observedAt、contentHash；正文按当前权限再取 |
| StepSummary | id、logicalStepKey、attempt、kind、name、status、startedAt、finishedAt?、durationMs?、publicInputSummary、publicOutputSummary、sourceIds[]、usage?、errorCode? |
| UsageSummary | runCount、statusCounts、terminalCount、successRate（0～1或null）、chat 的 inputTokens/outputTokens/estimatedTokens/unavailableUsageCount、byKind（chat/embedding/rerank 分组的计量）、queueDuration/executionDuration 的 count/avg/p50/p95、rangeUtc、timezone、scope；估算量单独标识，不混成实测 |
| Coverage | documentIds、chunkIds、retrievedChunkCount、usedChunkCount、truncated、limitations[]；GitHub 增加 repoId、queryCriteria、fetchedAt、pagesFetched、observedIssueCount、observedPrCount、hasMore、partial；不将样本数称全仓总量 |

### 三类任务输入和输出

| taskType | input 必填 / 可选 | 结果结构 |
| --- | --- | --- |
| qa | question；knowledgeBaseIds（至少一个）；documentIds?（空表示所选库中所有就绪文档） | answer、claims[{text,kind=fact/inference/suggestion/unknown,sourceIds}]、missingInformation[]、conflicts[]、coverage |
| compare | requirementDocumentId、solutionDocumentId；focus?；question?（追问） | summary、items[{requirement,requirementSourceIds,solutionEvidence,solutionSourceIds,status=covered/partial/not_found/conflict,impact,suggestion}]、coverage；缺方案证据时 solutionSourceIds=[] 且明确未检索到 |
| progress | repoId、startDate、endDate、timezone、knowledgeBaseIds；focus?、question? | progress[]、blockers[]、risks[]、toVerify[]、suggestions[]；每项 text、kind、sourceIds；另有 queryScope、coverage，事实和推断分开 |

compare 的两个文档必须不同、均就绪、在 Agent 当前绑定知识库内；progress 的知识范围至少有一份就绪文档。默认日期由页面按用户时区生成最近七个自然日，服务端再次验证。所有新增轮次重新计算有效范围；历史消息本身不是当前资料授权。

引用标签由服务端根据 run_sources 分配，不接受模型自创链接。文档事实必须使用本次取回的 chunkId；进展事实引用实际取得的 Issue/PR。结构校验通过也不代表语义一定正确，语义质量用固定评测集验证。

## SSE 载荷与客户端规则

采用 fetch + ReadableStream 的 SSE 消费方式，以便携带组织头、CSRF 以外的只读请求头和显式 Last-Event-ID；不用把会话 token 放 URL。必须正确处理 UTF-8 分片、跨网络块的行、多行 data、空行分隔、注释心跳及断线重试。[SSE 格式依据](https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events/Using_server-sent_events)。

| type | data 字段 |
| --- | --- |
| run.started | startedAt、deadlineAt、configVersion、taskType |
| message.delta | messageId、delta、isPartial=true；只展示生成中内容，不当作已校验最终答案 |
| tool.started | stepId、toolKey、attempt、publicInputSummary |
| tool.completed | stepId、durationMs、publicOutputSummary、sourceIds、coverage? |
| tool.failed | stepId、errorCode、message、willRetry；必需工具最终失败不改写成 completed |
| run.completed | messageId、resultRevision、finishedAt、usageSummary；客户端获取 AP54 最终快照替换草稿，不把草稿再次拼接 |
| run.failed / run.cancelled / run.timed_out | finishedAt、errorCode、message、partialMessageId?、usageSummary |

data 均额外带 `schemaVersion=1`。重连先查 AP54，获取一致的 lastSeq 与该序号对应的消息快照，再订阅大于 lastSeq 的事件。进程恢复从 DB 读取连续 seq；SSE 通知丢失不影响终态读取。草稿合并持久化和 lastSeq 更新属于同一事务。终态已存在且游标追平时正常结束流。`EVENT_CURSOR_EXPIRED/INVALID` 在开始 SSE 前返回 JSON 错误；流中鉴权失效直接关闭，客户端重读会话/快照并清除受限内容。

组织切换/注销会中止当前页面订阅与请求，清理对应缓存，不取消后台 run。前端重新聚焦、重连和每个权限轮询周期检查 accessRevision；收到变化立刻清缓存并重新取数。后端每批事件、每次工具调用前后及最终提交时检查当前权限，前端轮询只改善正在展示的旧页面。

## 错误、撤销和下载语义

| 错误族 | HTTP / 运行结果 | 页面行动 |
| --- | --- | --- |
| AUTH_REQUIRED / MEMBERSHIP_REVOKED / RESOURCE_NOT_FOUND | 401 / 403 / 404 | 重新登录、切组织或关闭失效内容；不得自动发模型调用 |
| VALIDATION_ERROR / NAME_EXISTS / DOCUMENT_NOT_READY / AGENT_NOT_READY | 422 / 409 | details.fields 定位字段，保留输入；readiness 给缺失条件 |
| IDEMPOTENCY_CONFLICT / VERSION_CONFLICT / CONVERSATION_BUSY | 409 | 核对原请求、刷新版本或打开当前运行，不换键偷偷重发 |
| FILE_TOO_LARGE / FILE_EMPTY / FILE_TYPE_UNSUPPORTED | 413 / 422 / 415 | 文件级错误；其他上传独立处理 |
| PDF_ENCRYPTED / PDF_INVALID / DOCUMENT_NO_TEXT / INGEST_LIMIT_EXCEEDED | 文档 failed | 修正文件或重试；扫描件不静默 OCR |
| MODEL_AUTH_FAILED / MODEL_UNAVAILABLE / TOOL_FAILED / TOOL_DENIED | run failed 或配置测试错误 | 检查连接/范围；授权错误不重试，暂时故障有限重试 |
| BUDGET_EXCEEDED / RUN_QUEUE_TIMEOUT / RUN_INTERRUPTED | run failed | 缩小范围或新建关联重试；原预算不可重置 |
| RUN_DEADLINE_EXCEEDED | run timed_out | 展示部分结果与重试入口；迟到结果不可改终态 |
| CITATION_INVALID / REQUIRED_SOURCE_UNAVAILABLE / PERMISSION_REVOKED | run failed | 修复/补充资料；不输出伪装完整的最终结果 |
| SOURCE_DELETED / SOURCE_REVOKED | 来源 tombstone / 403 | 删除保留历史答案但无原文；GitHub 撤销隐藏含该来源的整个回答正文、草稿及步骤结果 |
| EVENT_CURSOR_EXPIRED / EVENT_CURSOR_INVALID | 409 / 422 | 重新获取同 run 快照及消息，不创建新 run |
| CAPACITY_EXCEEDED / RATE_LIMITED | 429 | 提示当前限额与 retryAfterSeconds（可确定时）；不自动无限重试 |

resultVisibility 为 `visible/partially_unavailable/withheld`：删除文档为 partially_unavailable，原回答可留存；仓库、GitHub 连接/工具撤销涉及该轮则为 withheld。新 run 对已撤销资料不得继续生成，旧导出无法从用户设备追回。下载每次鉴权并通过 API 代理，Markdown 只含允许的来源与说明；无原始 HTML，链接只允许 http/https，外链新窗口加 noopener/noreferrer。

## 部署默认值与计量口径

下列是首版可实现的默认值，D01/D19 以代表性样例验证后可按变更流程调整；不是已测容量。系统硬值只由部署配置控制，M7 仅能降低表中标明“组织可降低”的项目。

| 键 / 作用 | 默认值与语义 |
| --- | --- |
| maxRunSteps / maxToolAttempts / runDeadlineSeconds / maxRunTokens | 沿用上方 10 / 10 / 120 / 16000；组织可降低，正在执行的 run 继续其快照；当前权限始终重新检查 |
| maxUploadBytes | 20 MiB；组织可降低；API、代理、前端同时约束，以服务端实际读入为准 |
| maxActiveRunsPerOrganization / maxQueuedRunsPerOrganization | 2 / 20；组织可降低；其他会话可排队，不能因一个会话运行而锁死整个界面 |
| maxQueuedRunsPerUser / queueTimeoutSeconds | 5 / 300；超过排队期限为 failed，不消耗执行期计时 |
| runLeaseSeconds / heartbeatSeconds | 30 / 5；取回租约 generation 递增，旧 generation 不能更新业务状态；剩余总时限不重置 |
| maxRecoveries / ingestDeadlineSeconds | 2 / 600；run 恢复仍受原 120 秒限制；文档解析在隔离 worker 中执行，有内存/时限限制 |
| worker 并发 | run 总并发 4、ingest 总并发 2；每组织 ingest=1；Windows 使用 Linux 容器运行 worker，不能把开发机进程池差异当可靠性证据 |
| history / retrieval | 最多最近 6 轮、历史最多 2000 token；D05 片段上限 384 UTF-8字节、重叠最多48字节（保守上界，非实际token）；向量/关键词各取 12 候选，融合后最多 12，重排取 8；证据上下文最多 4000 token；比较两侧分别分配，裁剪必须披露 |
| rerank 输入 | 每个候选最多 160 token 的查询相关摘录，保留回原 chunk 的映射；截断只能损失召回，不能改造原文，输出覆盖说明 |
| 候选流解码 | 一行一个 JSON 结果块，单行缓冲最多 64 KiB、整轮最多 64 块；聊天 Token 总预算仍是更外层硬限制；超限/解析失败转结果校验错误，不执行其中指令 |
| GitHub 查询 | 每页 25，Issue/PR 合计最多 4 页、100 个去重条目、额外详情最多 2 次；每次 MCP 网络尝试计入工具预算，重试也计数 |
| 持久事件 / 轮询 | 文本增量最多每 500ms 或累计 1KiB（先到者）刷入 DB；API 等待新事件轮询 1s，心跳 15s；前端 accessRevision 轮询 5s，隐藏页暂停、重新聚焦立即检查 |
| session / login 限流 | 会话绝对 8h、闲置 30min；lastSeen 写入节流 60s；失败登录按账号标识+IP 每 15min 最多 10 次，注册同 IP 每小时 10 次；不透露账号存在性 |
| 数据规模保护 | 每组织有效文档最多 1000、片段最多 50000、run 记录最多 10000；达到时明确拒绝继续新增，部署管理员维护归档；不偷偷丢历史。有效文档数包含 queued/processing/ready/failed 且未删除 |
| 清理 | 删除正文/向量/对象及孤儿临时对象目标 24h 内清理；checkpoint/SSE 事件终态后 7 天清理；已失效会话 7 天；审计/运行诊断日志 30 天；失败清理可重试并告警 |

run Token 预算覆盖聊天模型的所有输入与输出（包括工具定义、历史、检索上下文、重试及结构修复）。查询 Embedding 与重排独立按 usage_entries 记录，不混入聊天 Token 或省略收费尝试；它们受工具次数、输入长度、并发和时限约束。M6 明确分开展示 chat/embedding/rerank，不能把不同服务的计量混成“模型真实总 Token”。这解释了既有“输入与输出预算”的执行口径，不新增货币账单。

聊天调用前按适配器 tokenizer 计算输入并预留 maxOutputTokens（默认 2000，不能超过剩余预算）；提供方必须支持限制输出。D03 实现部署允许的模型/tokenizer 映射与能力测试，不能准确界定预算的模型不得标为可用。OpenAI 使用 tiktoken 映射；qwen-plus 使用官方 SDK 内置词表，标识 `dashscope-qwen-1.26.7`，profile 固定 `enableThinking=false`。D03 固定短输入探针用请求 UTF-8 字节数加余量保守预留、输出限制 64，百炼发送 `max_tokens` 和 `enable_thinking=false`；通用运行的输入预算计算由后续运行任务落地。调用已发出但 usage 未返回时按预留量保守记账，标为估算；已确认未发出才可释放预留。实际 usage 若超过预留立即停止后续调用并记异常，不能宣称已产生消耗能被追回。失败、取消、重试均保留独立尝试。

清理不替代权限撤销：逻辑删除/移除成员在事务提交后立即由服务端阻断读取；前端可能已合法显示或用户已保存的内容无法远程抹除。历史 run/消息/来源元数据保留，容量保护避免无限增长。实际计时以服务端 UTC 时钟为准，客户端仅展示。
