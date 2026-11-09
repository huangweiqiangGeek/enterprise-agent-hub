# D07 Agent配置与不可变版本

## 目录导航

- [范围与前置](#范围与前置)
- [方案与实施顺序](#方案与实施顺序)
- [验收映射](#验收映射)
- [执行证据](#执行证据)

## 范围与前置

2026-11-08，用户授权实现并验收D07，明确不推进D08。CP1产品事实源为PRD M3-01～07、contracts AP33～38、data-model Agent、pages M3和development-plan D07。D03模型连接、D05当前文档版本、D06检索代码均已读取；共享IdentityService组织锁、SessionProof和幂等机制可复用。源码无Git，不提交推送。

有界清点134项、1170360字节、最大173908字节，按后端/页面/测试/文档分批；任务前源码快照140项（含既有Web测试、排除pyc与依赖目录）已保存至项目运行态d07-before.zip。数据库/Redis/S3健康、已有worker运行；不把昨日测试当作本轮通过。

范围解释：M3-07真实会话/试运行属于D08/D09，M3-06在途停止需D08/D10，GitHub连接及工具执行属于D13。本次显示已保存版和未开放说明，不伪造run；progress可保存停用草稿，缺真实GitHub能力时不能启用。后续完整产品验收仍需对应任务。

治理候选GI-67A9DFD5-23453293-01：既定功能实施，record.none；未提出通用治理改变。机器路由仍投影dev.docs，实际采用dev-default开发流程；正式机器CP/终态保持UNVERIFIED，不自行修改控制面。

## 方案与实施顺序

CP2方案：API→AgentService→AgentRepository/ORM，沿用现有短事务身份/组织锁，不调用模型。新增agents、agent_versions、agent_version_kbs、agent_version_tools；复合组织FK、版本号唯一、当前版本FK。版本记录保留name/description/prompt/model及关联资源快照，保存追加新版本，历史版本与绑定禁止UPDATE/DELETE；task_type数据库与服务双重固定。启停只改enabled/revision/disabled_at并递增组织accessRevision，不制造内容版本。

AP33～38按现有契约落地；另加管理员GET /agents/options提供三模板与固定工具目录，避免前端复制另一份定义。成员只读启用摘要；完整配置仅管理员。创建/复制使用组织+用户+操作幂等键，PUT/status使用expectedRevision。UUID/列表长度/重复/未知工具/跨租户引用在服务端拒绝，停用允许空配置但不允许伪造资源。启用检查提示词、知识范围/当前就绪资料、已测试Chat/Embedding/Rerank及任务必需工具；readiness返回可定位字段。GitHub固定工具可绑定为草稿，当前均不可用。

知识库删除现有预留查询引用不存在的agents.deleted_at，本次移除此条件；当前版本绑定（包括停用配置）阻止删除，历史版本引用不阻止。复制剔除失效知识库/模型/不可用工具，说明剔除项；复制不保存凭据。

UI沿用现有workspace/表单/列表样式：列表筛选分页、创建模板、成员摘要、管理员五组编辑、复制命名、启停确认、readiness、版本号、脏表单离开/刷新提示。保存失败保留输入，409显式重载；组织切换取消旧请求，凭据不进入表单。未开放试运行按钮清楚说明未来使用已保存版及用量。

PR-1需求映射和PR-2～7技术/授权/兼容/负向路径复审：R3全面，需验证不可变版本、并发、租户/成员、资源删除、生成DTO、页面生命周期。无新增依赖/框架/外部API，沿用0003不可变版本与0004授权模式；不需要升级锁文件。当前方案无业务阻断。更简单选择为现有服务+4表，不增加事件溯源框架或Agent执行器。

CP3实施顺序：①模型/0006迁移/DTO/固定目录/service/API及真实数据库测试；②生成契约/页面/浏览器测试；③全量回归、隔离迁移降级重升、体验API更新、docs/Profile/报告/记忆/ECR。新增表为空，迁移只增量DDL；主库只升级，降级仅隔离验收库验证。回滚时先停止新API入口，保留新增表及业务数据，再回退本次代码；不得降级主库删除已保存Agent。

ExecutionContract：allowedFirstBatch=backend/migrations/tests；allowedPaths追加apps/web、生成类型、docs/README、项目Profile/报告/记忆；blockedScope=D08+、GitHub接入、真实执行、依赖升级、Git/发布、其他项目/用户数据清除。已有公共消费者为知识库删库检查、组织权限、模型引用和workspace导航，均纳入回归。跨10文件分批checkpoint，单写者，不派生Agent。ProfileImpactCheck需同步0006、Agent领域、页面/测试入口。

## 验收映射

| 条款 | 验证路线 |
| --- | --- |
| M3-01 | 成员列表/直链、跨组织、管理员完整配置投影 |
| M3-02 | 空白/三模板；模板Prompt可改，task_type创建后固定 |
| M3-03 | 草稿缺项、伪造资源、readiness、已就绪与模型/工具条件 |
| M3-04 | 不可变历史/关联、并发PUT一成功一409、保存失败保留、脏表单 |
| M3-05 | 复制幂等、重新命名、停用、失效引用剔除、无凭据 |
| M3-06 | 启停revision/accessRevision；在途效果待D08/D10 |
| M3-07 | 已保存版本及脏表单提示；真实试运行待D08/D09 |
| 必要关联 | 当前Agent绑定阻止删库、解除后可删、历史引用保留 |
| 工程 | pytest/ruff/mypy、pnpm lint/typecheck/unit/build、隔离浏览器、OpenAPI/类型漂移 |

## 执行证据

2026-11-08验收结果：D07本阶段配置能力已实现并通过下列验证；不代表D08执行闭环、M3-06在途停止或M3-07真实试运行已完成。

| 命令/入口（源码根执行） | exitCode | 实际结果 |
| --- | --- | --- |
| `uv run pytest -q` | 0 | 100 passed、3 skipped，74.28秒；跳过需显式真实提供方开关的Bailian/Linux ingestion/检索专项 |
| `uv run pytest tests/test_agents.py -q` | 0 | 6 passed；真实PostgreSQL/S3、受控模型协议，含降级至0005再升级0006 |
| `uv run ruff check backend scripts tests migrations` | 0 | 全部通过 |
| `uv run ruff format --check backend scripts tests migrations` | 0 | 85文件格式通过 |
| `uv run mypy` | 0 | 78 source files通过 |
| `uv run python -m scripts.export_openapi --check` | 0 | OpenAPI与当前后端一致 |
| `pnpm api:types`及前后SHA256比较 | 0 | 生成类型无漂移 |
| `pnpm lint`、`pnpm typecheck`、`pnpm format:check` | 0 | ESLint、Nuxt类型与Prettier通过 |
| `pnpm test:unit` | 0 | 2 passed |
| `pnpm build` | 0 | Nuxt生产构建通过 |
| `uv run python -m scripts.browser_smoke --node <pnpm Node路径> --model-fixture --storage` | 0 | 设置`EAH_BROWSER_INGESTION=1`，15场景通过，23.1秒；临时库/桶/API/生产Web |
| `uv run python -m scripts.probe_database` | 0 | PostgreSQL18.6、pgvector0.8.7；空库升级/重复升级、vector查询、checkpoint中断后新连接恢复通过，临时库删除 |
| `validate-profile.js --profile-dir <项目Profile> --project-root <源码根>` | 0 | Profile校验通过 |

后端专项覆盖：空白/三模板、缺项草稿、创建及复制幂等、伪造资源/额外凭据字段拒绝、当前版本绑定阻止删库、解绑后删除不破坏历史；并发PUT一成功一409；数据库拒绝版本/关联更新删除和task_type修改；停用不增版、成员直链拒绝、readiness动态复核模型/资料、复制剔除失效模型/库/工具。没有创建run表或调用模型生成答案。

浏览器专项覆盖模板可编辑、类型固定、脏表单取消离开、readiness字段定位保留输入、并发409保留与重载、命名副本独立且停用、启停与成员SSR摘要/不可用直链。390px与1280px截图已目视检查，无横向溢出。首轮验证发现SSR事件绑定前模板选择及新建成功跳转前启停按钮的竞态，已用交互就绪禁用和详情专属操作区修复；另修正测试中textarea文本/值断言及精确label定位。测试准备阶段既有上传页面出现一次早期文件选择未接受，D07改为真实上传API准备资料，既有上传UI仍由D04/D05场景覆盖，未扩改该页面。

## 本机体验与清理

主库`eah`从0005升级0006，二十张app表；迁移前后原有十六张表行数一致。主库无降级、无测试数据写入、无卷删除。API重启前核对旧PID39580/监听11892的命令和启动时间，停止旧进程树并确认8300释放；新API根PID15184、监听10664、2026-11-08T21:04:21.9266610+08:00启动，命令`uv run --locked python -m backend`，cwd为源码根。原Web根37064/监听14680、端口3300继续保留。API和Web代理ready=200，实际OpenAPI包含AP33～38/options；未登录页面请求200是登录重定向后的页面，不作为登录后功能验收证据。

体验入口：`http://127.0.0.1:3300/o/f31f7524-6abd-43eb-b149-e0772fcaeba1/agents`。依用户此前要求保留体验服务，关闭前核对PID/启动时间后停止对应进程树；Compose需要关闭时执行`docker compose --profile storage --profile ingestion stop`，保留卷。已有PostgreSQL/Redis/S3 healthy，ingestion/dispatcher保持原容器运行；D07未改worker代码，未重新宣称真实worker/百炼验收。

最终浏览器临时API PID4100端口55443、Web PID39776端口55444，由隔离脚本启动并在finally停止，端口释放断言通过，临时数据库/桶已删除。此前失败尝试同样完成清理；日志与截图归档到项目运行态。常驻环境记录同步到`.runtime-state/experience-services.json`。

## 复审与未验证边界

ReviewGradeCard：全面R3，风险为多模块/公共契约/权限/迁移；业务复审PASS，依据为上述契约、实际代码差异、正负向数据库测试、浏览器及主环境读回。29个源码/文档文件变更，零删除，未改依赖锁或D08实现；无Git仓库，以任务前快照对比留证。docs、生成OpenAPI/类型、Profile已同步。最终报告和项目日记/SUMMARY参与收尾核对；机器正式CP/ReviewExecution/terminal未取得受管回执，保持UNVERIFIED，不将手写记录等同运行时认证。

本轮不新增依赖/SDK/外部API，因此依赖官方文档前置N/A；配置使用已有D03连接和D05资料事实。模型fixture只验证协议/流程。真实模型、Linux ingestion与检索专项本轮未重跑；此前证据保留在对应阶段报告，不能算本轮新验证。DashScope导入仍有既有Assistants弃用警告。

唯一后续任务为D08「持久运行与T1 Agent执行」，须另行授权。它应在接受run时固定Agent版本与模型版本，落实预算/工具授权及停用检查；D09接入真实试运行入口，D13接入GitHub。此次不自动推进。
