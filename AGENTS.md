# Enterprise Agent Hub 开发入口

完整规范在 [docs](docs/README.md)。先识别本次任务，再按下表读取；不要求每轮全量加载所有文档。

| 任务 | 必读 |
| --- | --- |
| 初始化、模块拆分、技术选型、数据归属 | [架构边界](docs/architecture.md)、[质量规范](docs/quality.md) |
| 页面、组件、样式、前端状态 | [前端规范](docs/frontend.md)；编码与验收读 [质量规范](docs/quality.md) |
| API、权限、数据库、上传、worker | [后端规范](docs/backend.md)、[契约](docs/contracts.md)、[质量规范](docs/quality.md) |
| RAG、Agent、MCP、模型、Prompt、记忆 | [Agent 规范](docs/agent.md)、[契约](docs/contracts.md)、[质量规范](docs/quality.md) |
| API 字段、运行状态、SSE 或跨层调用变更 | [契约](docs/contracts.md)及涉及的前端/后端/Agent 规范 |
| 仅修改文档 | [文档索引与维护规则](docs/README.md)及被修改主题；检查反向引用 |

任务跨多类时读取对应集合；仅改页面样式不要求加载 Agent 全文。测试、依赖及部署变更按 [质量规范](docs/quality.md) 读取对应章节。规范与代码冲突时先核实事实，不静默改变契约。

使用 DevCodex 时，在本地工作区绑定项目 Profile；本项目 docs 始终是规范正文，不创建另一份规则副本。
