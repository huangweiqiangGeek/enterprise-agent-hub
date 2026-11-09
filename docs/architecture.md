# 架构与范围

本文用于初始化、模块拆分和边界变更。目标是一个 Nuxt 前端和一个模块化 Python 后端，API 与 worker 可独立运行，但复用同一业务代码库。先做可验证的单项目闭环，不拆出独立 RAG 或 Agent 微服务。

## 技术基线与目录

沿用 Nuxt 4、Vue 3、TypeScript、Tailwind CSS、shadcn-vue；后端使用 FastAPI、LangGraph、PostgreSQL/pgvector、Redis、Celery，文件存储走 S3 兼容接口。技术方案选择 SQLAlchemy 2.0 系列、psycopg 3、Alembic 与 uv；运行环境、适配器和选择依据见 [完整技术方案](technical-design.md)。精确依赖版本在 D01 核对官方兼容性并实测后锁定；不把机器当前安装版本当作项目要求。

| 规划位置 | 职责 | 不承担的职责 |
| --- | --- | --- |
| `apps/web/app/pages`、`components`、`composables` | 页面、交互、请求与订阅生命周期 | 鉴权裁决、模型调用、业务状态流转 |
| `backend/api` | HTTP/SSE、认证上下文、DTO 转换 | 直接编排复杂 Agent 或解析大文件 |
| `backend/services` | 用例、权限校验、事务边界 | 返回框架响应对象 |
| `backend/repositories`、`models` | 带组织范围的数据访问与持久化 | 决定用户权限或调用模型 |
| `backend/agent` | 执行图、预算、停止、恢复、结果整合 | 绕过工具层直接访问任意资源 |
| `backend/rag` | 解析、切片、向量化、检索和重排 | 扩大调用者可访问的知识范围 |
| `backend/tools`、`mcp` | 工具定义、参数校验、授权与适配 | 从模型文本获得权限 |
| `backend/workers` | 消费持久化任务、重试、进度回写 | 使用另一套业务规则 |
| `migrations`、`tests`、`docs` | 数据迁移、验证、规范 | 保存业务运行数据 |

目录按功能落地，不为尚无消费者的模块预建空框架。Nuxt 服务端最多做必要代理和页面渲染，业务 API 归 FastAPI。部署入口将 `/api/v1` 和事件流转发给后端；代理不得缓冲 SSE。

## 依赖方向与数据所有权

1. HTTP/API 与 worker 调用 services；services 调用 repository、Agent/RAG 接口。领域逻辑不依赖 FastAPI Request 或 Vue 对象。
2. Agent 通过有类型的工具调用业务能力；工具适配器复用 service 的权限和数据规则。工具结果不能自行变更执行预算或用户身份。
3. PostgreSQL 是业务记录、任务状态和运行事件的事实源；Redis 用于队列、缓存或临时协调，不作为唯一业务存储。
4. LangGraph checkpoint 属于运行恢复基础设施，不能替代组织成员、权限、最终消息和审计记录。checkpoint 的表结构由其选定适配器管理，不与业务迁移争抢同一张表。
5. 对象存储只保存文件内容；文件归属、解析版本、删除状态在数据库维护。数据库 ID 不能直接拼成任意文件系统路径。
6. 两个个人项目分别使用自己的数据库、迁移、队列前缀和存储命名空间，不互相直接查表，不提前建立共享业务包。

## 请求与异步任务

上传请求完成身份、大小和类型校验后持久化文档记录与待处理任务，再返回接受结果。worker 解析并切片，执行 Embedding 后标记可检索。失败状态必须可查询；队列不可用时不能丢失已接受任务，使用数据库待投递记录补偿。

创建 Agent run 时固化配置版本、身份范围和预算，再由运行 worker 执行。前端订阅运行事件并查询权威快照；关闭页面不意味着取消运行。取消必须走显式 API。

## 范围与演进条件

第一版仅接入管理员配置的只读工具与至少一个真实 MCP 工具。任意 URL 抓取、上传后执行脚本、写外部系统、多 Agent 委派均须另行定义需求、权限和验收。只有出现独立扩缩容、部署或复用需求时才拆服务；先用实际性能和故障证据说明必要性。

角色、具体状态及执行预算以 [contracts.md](contracts.md) 为准。代码与任务验收见 [quality.md](quality.md)。

完整实施设计按需读取：[数据模型](data-model.md)、[页面与交互](pages.md)、[Agent 执行流程](agent-flow.md)、[开发任务与验收](development-plan.md)、[Codex 提示词](codex-prompts.md)。本文件维护长期职责边界，具体首版落地选择由这些文档承接，不重复维护接口和限额。
