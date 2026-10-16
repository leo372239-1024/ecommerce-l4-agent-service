# ADR 汇总

2026-10-09 复审方向已确认，ADR-010 起优先于原简版中冲突内容。以下核心条目是已采用的设计决策，实施进度见 [步骤 3 交付审查](../delivery/step3-infrastructure-review.md)，不能由设计决策推断 Agent 业务已实现。依据见 [调研复审](../research/2026-10-09-agent-core-review.md)、[核心设计 v2](../architecture/agent-core-v2.md)。

## ADR-001：LangGraph + PostgreSQL Checkpointer

决策：LangGraph 作为主 Agent 编排框架，PostgreSQL 保存 Checkpoint 和业务状态。

原因：需要可恢复执行、HITL、中断恢复、长任务和轨迹回放。内存 Checkpointer 只用于单元测试。

## ADR-002：PostgreSQL 与 Elasticsearch 分工

决策：PostgreSQL 是事实数据源，Elasticsearch 是搜索和检索索引。

原因：事务状态、幂等和检查点需要关系型约束；商品、FAQ、政策和案例需要 BM25 + 向量混合检索。

## ADR-003：REST + SSE 为主，WebSocket 为辅

决策：REST 负责控制面，SSE 负责 Agent 单向事件流，WebSocket 保留给实时工作台和双向协作。

原因：Agent 事件天然是服务端到客户端的有序流，SSE 易于浏览器接入、断线重连和代理部署。

## ADR-004：Redis Streams MVP，Kafka Adapter 后置

决策：MVP 使用 Redis Streams + Outbox；生产扩展提供 Kafka EventBus 实现。

原因：Kafka/RocketMQ 在单机 MVP 中运维成本过高，且消息 Exactly-Once 不能替代业务幂等。通过接口隔离保证迁移路径。

## ADR-005：OpenTelemetry 为观测标准

决策：所有 API、Worker、Agent、MCP 和模型调用使用 OTel Trace/Metric/Log。

原因：供应商中立，便于导出到 Langfuse、Prometheus、SkyWalking 或其他后端。

## ADR-006：Casbin RBAC + ABAC

原决策：FastAPI OAuth2/JWT 负责身份，Casbin 负责 RBAC 与 ABAC。复审修订见 ADR-015；Casbin 授权方向保留。

原因：Spring Security 和 Apache Shiro 属于 Java 生态；Casbin 有 Python 实现并支持 RBAC、ABAC 和策略运行时管理。

## ADR-007：多层护栏而非单一安全模型

决策：规则、Schema、轻量分类器、LLM 安全检查和 HITL 分层执行。

原因：低风险输入不应承担安全大模型的固定延迟和成本；高风险操作必须由确定性策略和人工审批兜底。

## ADR-008：主流程确定性，局部步骤自适应

决策：顶层使用固定状态机和 Plan-and-Execute，主步骤内部使用受控 ReAct 子图。

原因：客服任务必须沿业务主线执行，同时允许局部工具失败和环境变化触发重规划。

## ADR-009：不输出原始思维链

决策：前端只展示计划、工具调用、结果摘要、风险依据和重规划摘要。

原因：保护系统提示词和内部推理信息，同时满足用户可观测和审计要求。

## ADR-010：单 Agent 混合执行与可验证计划

提案：直接 LangGraph 构建薄业务 Harness，业务状态/金额/准入确定性执行；同一 Agent 生成类型 DAG 并局部探索。使用受控 predicate 与工具效果契约校验计划，不再串联第二套 Chain 主流程。简单查询走快捷路径。

## ADR-011：环境事实与上下文分离

提案：业务事件使快照失效，按需刷新，写入时执行 expected_version/CAS；来源与时效随证据进入上下文。事件、历史记忆、检索索引都不能独立授权写操作。

## ADR-012：分层工具检索与参数绑定

提案：权限/资源/状态过滤先于 BM25+dense/RRF，按需要 rerank 短列表，最终按 schema/业务效果检查。工具目录冻结版本并评测 50/200/1000 档；缺参澄清，无匹配不虚构工具。

## ADR-013：四层记忆与 AWM 晋升

提案：checkpoint 工作状态、episodic、semantic、procedural 分层；PG 为持久事实，LangMem 原语可替换。AWM 只从开发/已验证轨迹抽取，经过回归/人工审核，生产与盲测冻结；新政策/工具版本使技能失效。

## ADR-014：外部反馈 Reflection 和有界循环

提案：每步程序检查，有错误或冲突才做证据反思；按错误类型处理，未知写结果对账；进展签名、deadline、工具/模型/Token/重规划上限联合约束。反思不能修改业务真值或绕过授权。

## ADR-015：共享浏览器会话与标准 AG-UI

提案：同源浏览器用安全 session cookie 与服务端共享会话/CSRF；后续外部身份采用 OIDC Code+PKCE。REST+AG-UI SSE 为 MVP 通道，WS 后置。使用官方事件 SDK 和中断生命周期，workflow_id 与 protocol run_id 分开。

## ADR-016：业务操作账本与可恢复调度

提案：PG transactional Outbox/Inbox，Redis 通知至少一次，PG 待执行任务可补偿；租约/fencing在实际业务写入口核验。稳定 operation_id + 参数hash + 唯一约束 + 业务状态机 + 回执对账。checkpoint不能单独保证外部副作用Exactly-Once。

## ADR-017：检索与观测按资源 profile 部署

提案：ES负责BM25、pgvector负责dense、应用RRF；ES/本地检索推理与完整观测栈不默认全部运行。本机约16GiB/Docker9.7GiB、4CPU，需要实际Docker资源验证。当前主干/最新release快照只作候选依据，安装和镜像版本由兼容测试冻结。

## ADR-018：沙箱能力随可验证隔离边界开放

提案：MVP只开放固定、硬化的计算/解析工具，无Docker socket/宿主挂载/密钥/默认网络。普通Docker不能作为不可信任意代码充分隔离，任意shell/模型代码在验证gVisor/microVM前保持关闭。

## ADR-019：成果必须来自真实配对评测

提案：B0普通ReAct、B1较强检索ReAct、完整P及独立消融；同模型、政策、权限保护、预算和数据。至少3项深化主指标通过真实Docker实测及统计审查，不用外部论文数字填项目成果；80条候选规格必须转为可执行黄金集后才算正式交付。

## ADR-020：云端向量与精排，分别使用模型与接口

决策：用户确认采用百炼 `text-embedding-v4`、1024 维与 `qwen3-rerank`。Embedding 使用 `compatible-mode/v1/embeddings`，精排使用 `compatible-api/v1/reranks`，后者不传 dimensions。两类请求与响应分别验证，密钥只在服务端本地 Secret 中。已通过真实小样本接口探针，尚未测量中文知识召回质量。

原因：用户现有账号两模型均可用，本机无需同时部署向量模型和精排模型。原本地 Qwen/BGE 方案保留为后续对照，不作为本步默认；其他高性价比 LLM/API 已获允许，但必须经公平开发集评估、成本与注册说明后引入。主基线与改进方案固定同模型，另做模型消融，避免把换模型的增益计入架构深化。

## ADR-021：上传限制由 Python 服务与入口共同执行

决策：单文件与整请求上限均为 104857600 字节（100 MiB）。Nginx、ASGI 整请求限制及可复用文件流检查分别约束入口和读取；无 Content-Length 时仍按实际读取字节计数。multipart 边界及表单字段计入整请求，因此恰好 100 MiB 的文件加表单后会超限。此处按用户给出的两个相同限值实施，知识库上传业务在后续步骤接入这些约束。

## ADR-022：步骤 4 保持可持久化的只读执行边界

实现：模型只提交有原文实体证据和每订单目标范围的 TaskSpec；程序编译强依赖 DAG，五个只读工具重新校验客户归属、实体版本与金额事实。Reflection 只允许有限只读修复，政策拒绝与技术失败分开，写请求转人工。Run、事件/Outbox、PG 租约/fencing 与 checkpoint 会话锁形成可恢复执行；Redis Pub/Sub 只承担唤醒提示，PG 是权威。等待用户释放 worker，通过 typed interrupt + revision CAS 恢复。

原因：先真实验收复杂目标绑定、暂停恢复、动态环境、无进展停止及进程故障，再在步骤 5 开放 MCP/多工具路由和带账本/审批/CAS/对账的业务写操作。检查点不替代业务幂等，单机 Docker 不等于 HA，样本冒烟不等于基线提升。具体契约和实测边界见 [步骤 4 运行架构](../architecture/step4-runtime.md) 与 [交付审查](../delivery/step4-agent-core-review.md)。

## ADR-023：步骤 5 开放受控 MCP 与三类可对账副作用

实现：40项受控工具目录、权限优先的中文混合召回/精排和原生函数选择；三类写效果由确定性骨架编排，经持久提案/客户确认/额度审批后执行。稳定 operation ID 与参数 hash、当前主体/商家/fence、实体和政策版本在商家事务内复核，业务变更/回执/Outbox 同事务。恢复按 ID 查询原回执，默认历史查询有界。HANDOFF/PARTIAL 同事务生成可领取、核对和关闭的持久工单。

原因：工具相似度、模型自评、checkpoint 或客户端“批准”字段都不能独立授权资金操作。共享模拟事务可证明未提交；真实远端接入必须提供远端幂等/回执查询契约，不能套用本地未查到即未退款的推断。MVP采用 PG Outbox/Inbox，Kafka与多机 HA 后置。依据及边界见 [步骤5技术证据](../research/2026-10-09-step5-tools.md) 和 [业务架构](../architecture/step5-tools-and-effects.md)。
