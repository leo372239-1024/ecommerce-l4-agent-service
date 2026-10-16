# Agent 核心能力与全栈技术选型复审

调研日：2026-10-09。状态：**待用户审查；设计决策，尚未实现或测出收益**。

本次是步骤 1、2 的补充复审，暂停步骤 4。阅读本文件后，结合 [核心设计](../architecture/agent-core-v2.md)、[评测协议](../evaluation/benchmark-protocol.md)、[80 条用例规格](../evaluation/golden-case-catalog.md) 审查。通过后先修复步骤 3 的基础设施，再进入核心开发；继续遵守每个步骤完成后停下审查的约定。

## 1. 结论与业务定位

选择 **LangGraph 驱动的单 Agent、确定性业务状态机与有界推理结合、以业务事实验证执行结果**。前后端仍是 React + Ant Design、FastAPI + uv。框架、模型、检索与部署基础设施分别选型，避免因引入一个通用 Agent 产品而被迫接受整个技术栈。

业务闭环：中文售前咨询 → 身份与订单归属核验 → 订单/物流查询 → 改址、取消、退换货、补发、退款试算与执行 → 超权限审批或人工接管 → 执行结果对账 → 用户确认/工单关闭 → 可验证轨迹进入评测与候选工作流记忆。自主执行权限必须由业务策略定义，“L4”是本项目的产品定位，不是已取得的行业等级认证。

复杂示例：“两笔订单里未发货的改址，已签收破损的只退破损商品；优惠券是否退回也说明，不能退款就补发，超过额度转人工。”系统应按订单实体拆分、区分主请求与条件分支、询问缺失信息、读取政策与实时状态、试算、确认/审批、分别执行、对账，并明确部分完成情况。不能把退款和补发同时执行，不能根据旧聊天内容假设订单仍未发货。

“行业顶尖”作为验收方向，拆解为业务完成、复杂任务一致性、工具选择与参数正确、安全边界、恢复能力、用户可观测和运营能力。现阶段不能凭架构宣称超过 Agentforce、Dify，不能将外部论文的数字当成本项目成果。

## 2. 对标对象与证据边界

Salesforce 官方文档描述了 Agent Script → Agent Graph → Atlas 的混合执行架构，业务逻辑与模型判断有明确边界，动作可用性随状态变化。[S01] 这支持本项目使用业务图、条件准入和执行前后检查；**不意味着需要多 Agent，也不意味着复刻其闭源推理引擎**。

本项目与 Agentforce 的可审查对应关系如下，右列是设计目标而非现有能力：

| 企业客服能力 | 本项目实现方向 | 验证证据 |
|---|---|---|
| 业务控制与推理结合 | 可审查的政策/技能配置编译成 LangGraph 子图；模型仅提出计划与动作 | 非法状态跳转被拒、必需步骤覆盖率 |
| 基于企业事实回答 | 订单实时读取、政策带版本与生效期、答案关联证据 | 关键事实/金额与最终数据库状态一致 |
| 条件动作准入 | 权限、订单状态、政策、审批条件同时约束候选与实际执行 | 越权工具不曝光、不执行；TOCTOU 测试 |
| 执行与人工接管 | 持久化审批、暂停恢复、完整工单交接、后续对账 | 重启恢复、审批过期与重复请求测试 |
| 测试和运营 | 轨迹与业务判定器、故障注入、指标与回放 | Docker 原始轨迹、报告、可复现配置 |

Salesforce 产品信息属于供应商的一手能力说明，不能当独立横向质量榜单。Dify 可以在本地部署为外部参照，需锁定版本、相同工具/政策/模型与调参预算；没有可用 Agentforce 测试环境时，只对标公开能力，不能给出产品胜率。

## 3. 核心技术的横向比较

| 能力 | 候选比较 | 本次选择及理由 | 不采用/延后部分 |
|---|---|---|---|
| Agent Harness | LangGraph 精细控制状态与恢复；Deep Agents 已提供规划、技能、结果卸载；Dify 有产品化流程和运营；AgentScope/其他框架偏通用或多 Agent | **直接 LangGraph + 薄业务 Harness**。复用官方 checkpoint、interrupt、retry；业务准入与结果验收独立于提示词 | 不再串联第二套 LangChain Chain 主流程；不整包引入通用研究 Agent 的 shell/子 Agent |
| 环境感知 | 每步全量拉取简单但贵；事件流及时但可能丢失/乱序；快照读取适合校验 | **事件触发失效 + 按需最新快照 + 写前版本校验**。事件提示变化，业务 API 是事实源 | 无授权的桌面监控/浏览器自动化；仅凭事件投影提交退款 |
| 规划 | 普通 ReAct 简单；纯 Plan-and-Execute 容易僵化；HTN 控制强但模板维护多；任意 Tree-of-Thought/搜索开销大 | **业务骨架 + 类型化依赖 DAG + 局部受控 ReAct + 增量重规划**，简单查询走快捷路径 | MVP 不用无限分支搜索；不向用户输出原始 CoT |
| 大规模工具路由 | 全量 schema 容易挤占上下文；单次分类漏召回无法恢复；纯向量对近义工具不稳 | **权限/状态过滤 → BM25 与 dense 召回 → RRF → 条件性 rerank → 参数/效果契约校验** | 训练专用路由分类器延后，先收集标注、负样本和失败轨迹 |
| 长期记忆 | LangMem 有提取/整合原语；Mem0 集成广；Graphiti 有时间图与溯源；Letta 侧重自管理状态 | **LangGraph Store/PG + 显式领域记忆 schema，LangMem 作为可替换提取组件**。业务订单仍实时查询，用户记忆隔离且有来源 | 不同时安装多套记忆框架；Graphiti 在多跳关系检索产生可测需求后再引入 |
| AWM/程序性记忆 | AWM 从轨迹抽取可复用工作流；通用技能文件容易变成不可验证提示词 | **经验证的候选轨迹 → 参数化工作流 → 离线回归 → 人工晋升 → 版本化技能** | 不在线自动修改生产政策；不把客户自由文本升级为系统指令 |
| Context Engineering | 全历史成本高；纯摘要可能丢实体/条件；观察遮蔽更简单但需回取机制 | **固定关键状态 + 检索片段 + 近期完整工具对 + 历史结果引用 + 结构化摘要** | 不机械固定“最近 5 轮”或保留全工具列表；不把摘要当业务事实源 |
| Reflection | 纯自我批评易相关错误；独立大模型成本高；确定性判定器只覆盖可形式化部分 | **规则/数据库/金额校验 → 证据约束的模型反思 → 必要时人工复核** | 每一步都调用大模型批评；以模型自报 confidence 放行退款 |
| Loop | 框架 recursion limit 只限制图步数；无预算重试容易放大失败 | **明确状态机 + 进展签名 + 分类错误处理 + 多维预算** | 不把超时等同失败退款；不盲重试不支持幂等的写工具 |

Deep Agents 的技能、结果卸载、上下文管理可复用思想；其默认工具和子 Agent 必须按本项目边界裁剪。[S03] LangGraph 的 checkpoint 是运行状态，Store 是跨会话存储，二者不是同一种记忆。[S02] AWM 的研究场景主要是网页任务，必须在电商业务中重新验证迁移效果。[S05]

## 4. 整体全栈复审后的选择

“保留”表示保持方向；“新增/调整”仍需本次审查后才改变运行代码。准确补丁版本、镜像 digest、模型 revision 统一在步骤 3 修复时记录，**不把 GitHub main 的版本号冒充已验证发布版本**。

| 层 | 选择 | 本次调整和理由 |
|---|---|---|
| 前端 | React 19 系列、TypeScript、Ant Design 6、Vite 8 候选 | 保留 React/Ant Design 方向；升级旧 antd5/Vite6 约束，迁移/Node 兼容实测后锁定。Ant Design 官方 v6 文档明确支持 React19，见 S25。增加 TanStack Query 管理服务状态，AG-UI 客户端处理流；不引入整套重量级 Agent UI 平台 |
| API | FastAPI、Pydantic v2、httpx、orjson、uv | 保留；统一开发/容器 Python 3.12 补丁版本。uv.lock 冻结构建失败即失败；取消静默重新解析依赖 |
| ORM/迁移 | SQLAlchemy 2 async + asyncpg、Alembic；checkpoint 用官方 psycopg 驱动 | 保留，明确两种连接池分工与总连接预算；只由迁移作业修改 schema，Worker 不竞相迁移 |
| 分页 | fastapi-pagination；时间线/轨迹用 `(seq,id)` 或 `(created_at,id)` 游标 | 普通列表可 offset；高频事件与大表不使用深 offset |
| Agent | LangGraph 稳定 1.x 兼容组合、langchain-core、checkpoint-postgres | **升级旧 0.x 约束**。当前本地 LangGraph 代码为 1.2.12，远端 main 元数据为 1.2.14；实际安装版需核验发布与兼容。无需 LangGraph 托管服务 |
| MCP | 官方 Python SDK 稳定版 + Streamable HTTP；LangChain MCP adapter 可选 | 新增明确依赖与契约测试。远端发布快照为 v2.3.0；协议协商与迁移需实测，不复制旧 v1 示例。工具扩展元数据使用受控注册表/命名空间 `_meta` |
| 模型 | 智谱用户指定 endpoint 与 `glm-4-flash`，可配置 OpenAI-compatible 适配层 | 保留用户指定默认值。官方当前文档检索到 `glm-4-flash-250414`，**不能假设它与指定别名完全相同**。先测实际 tools、流式工具参数、JSON/schema、上下文、取消和限额；不静默换更贵模型 |
| 模型 Gateway | httpx/薄适配器、请求队列、限流、超时、熔断、费用/Token 账本 | MVP 一个已配置模型，无可用备用即明确失败/转人工；不编造多模型降级链。LiteLLM 在多供应商需求成立后引入，避免当前多一层代理 |
| 数据库 | PostgreSQL + pgvector；选择受支持大版本与扩展兼容组合 | 保留；18 系列是当前候选，16 系列仍可在兼容测试后选用受维护补丁。订单/授权/审批/幂等/记忆/运行元数据是 PG 真值 |
| 搜索 | Elasticsearch BM25；pgvector dense；应用层 RRF | **统一向量真值在 PG**，避免在 PG/ES 双写相同 embedding。ES 用独立 profile 做标准检索测评；core fallback 用 PG FTS/trigram，中文效果单独报告。不依赖尚未核验许可证可用性的 ES 内建 RRF |
| Embedding | 用户后续确认：百炼 text-embedding-v4，1024 维；本地 Qwen3/BGE 保留对照 | 已通过真实接口与向量维度探针，尚未验证领域召回质量。中文、schema/任务检索匹配在开发集评估；不能按公开 MTEB 名次替代项目测评。固定模型、维度与输入 instruction |
| Reranker | 用户后续确认：百炼 qwen3-rerank；本地 Qwen3/BGE 与无 rerank 保留对照 | 已通过真实精排探针；仅对近义冲突、低 margin、复杂任务短列表调用。接口失败时仍保留召回与硬校验，降级质量另报；云端限额及吞吐仍待测 |
| 队列/缓存 | Redis Streams + PG transactional Outbox/Inbox | 保留，不上 Kafka/RocketMQ。采用受维护 Redis 版本并登记许可证，不把 7.4 当 BSD。PG 是待运行任务与事件真值，Redis 是通知/调度加速；缓存不作授权/幂等唯一依据 |
| 用户认证 | 同源浏览器：HttpOnly Secure SameSite session cookie + 共享 session store；未来 OIDC Authorization Code + PKCE | **替换“分布式必选 JWT 双令牌”结论**。session 同样可以分布式；JWT/OAuth2.1 不是 Agent 专属要求。对公网 BFF 及 CSRF 做明确测试，不实现密码授权伪 OAuth2.1 |
| 授权 | Python Casbin + 显式业务 ABAC/资源归属；PG RLS 作为纵深隔离 | 保留。模型不能传任意 user_id/tenant_id 覆盖可信身份；执行前重新授权；工具服务也校验，不只前端隐藏按钮 |
| 通信 | REST 控制 + AG-UI SSE；WS 协作后置 | **MVP 移除没有业务需求的 WS 常驻通道**。AG-UI 标准事件由 SDK 校验，计划/审批用 CUSTOM/状态事件；断线按持久 seq 回放 |
| 护栏 | schema/长度/规则、授权/金额/状态、来源隔离、最小工具权限、HITL；检测器插拔 | 不把 NeMo/Guardrails AI/Moderation 的输出当业务授权。默认不在 3050 上部署大安全模型；待攻击样本与误拒率明确后比较小分类器/安全模型。正则不是注入防御的充分条件 |
| 沙箱 | MVP 独立、硬化的固定计算/解析容器；无需任意 shell 的业务工具 | sandbox broker 与 Agent 隔离，禁 Docker socket、宿主挂载和默认网络。**未验证 gVisor 时禁止不可信任意代码**；后续 Linux sandbox 节点采用 gVisor/OpenSandbox 或 microVM |
| 可观测 | OTel、structlog JSON、Prometheus/Grafana；Jaeger 2；PG 业务审计与轨迹 | 观测 profile 采用 Jaeger2，轻量开发可限额 memory store，技术trace重启会丢，业务轨迹仍在PG；持久trace配置受支持ES后端并验证兼容/资源。Tempo适合后续对象存储/规模场景，当前不再加对象存储依赖。LangSmith adapter可选，需要账号与脱敏策略；Langfuse自托管独立重型profile，不与多个tracing栈同时默认启用 |
| 评测 | pytest/httpx/属性测试、任务状态判定器、τ-bench adapter、BFCL 式参数判定、可插拔 Judge、k6、Playwright | pytest 测业务不变量；对 Agent 测轨迹/终态。新 τ³ 功能仍在 tau2-bench 仓库，不使用过时任务标签；锁定版本防止评分漂移 |
| 交付 | Docker Compose profiles；CI 镜像构建/SBOM/扫描/锁文件/迁移与恢复检查 | 本机 Compose 不等于生产 HA。后续 K8s/Helm、托管 PG/Redis/ES、多 AZ、灾备实测；Istio、Kafka、Harbor、Jenkins 无现阶段必需业务需求 |
| 日志平台 | 默认 JSON stdout + OTel；集中日志按需加 Loki | 不引入 ELK 全套只为打印日志；SLF4J/Logback 属于 Java 生态，不用于 Python。SkyWalking 可作为 OTel 消费者，当前无需并行维护 |

## 5. 模型与基础设施的现实边界

补充发行快照（GitHub `releases/latest`，不是最终安装清单）：[FastAPI 0.143.0](https://github.com/fastapi/fastapi/releases)、[uv 0.12.24](https://github.com/astral-sh/uv/releases)、[Ant Design 6.6.5](https://github.com/ant-design/ant-design/releases)、[Vite 8.3.4](https://github.com/vitejs/vite/releases)、[Redis 8.10.2](https://github.com/redis/redis/releases)、[Elasticsearch 9.5.5](https://github.com/elastic/elasticsearch/releases)、[Prometheus 3.15.0](https://github.com/prometheus/prometheus/releases)、[Grafana 13.2.3](https://github.com/grafana/grafana/releases)。各项目发行记录在本轮只读查询。选用稳定受维护的兼容组合，而不是一律追最新；ES Python client 必须与实际 ES 服务端大版本匹配。旧 ES8.15.3、Prom2.55.1、Grafana11.2.0、OTel0.108.0 等 scaffold 配置必须重新审核，当前没有扫描证据，不能直接断言存在某个 CVE。

指定 `glm-4-flash` 能否满足复杂规划与大量工具调用，目前没有本项目能力探针与盲测证据。保留默认模型，通过规则与 Harness 提升质量，但不能承诺框架会消除模型能力上限。对每个模型单独记录 schema 遵从、工具参数、多步骤成功、P95、限额与费用。任何模型切换都作为独立实验，不与架构收益混在一起；未配置第二模型时，失败后转人工/排队，不能声称自动多模型高可用。

本次只读环境核验：物理内存 16,869,548,032 bytes（约 15.7 GiB）；Docker 报告 4 CPU、10,429,775,872 bytes（约 9.7 GiB）。这不是压力测试。资源方案：

- `core`：前端、API、Worker、MCP、PG、Redis、必要 relay；云模型；暂定容器内存总预算 4–6 GiB，**只是预算**。
- `search`：ES；用户后续确认 embedding/reranker 改为百炼 API，不占本地模型推理内存。本地推理仅保留为后续对照 profile 候选。
- `observability`：Collector、Prometheus、Grafana、trace 后端，暂定增量 1–2 GiB。Langfuse 自托管另设 profile，其 PG/Redis/ClickHouse/blob 依赖必须按实际版本核验。[S15]
- `eval`：评测器与 k6 独立进程。真模型评测从 1 并发开始；基础设施并发测试用可控模型桩，结果分开报告。

当前机可做 MVP 功能与单机负载验证；同时全开 ES、两个本地模型、完整 Langfuse、压测器时有明显资源竞争可能。若出现 OOM、持续 swap、容器 throttling 或数据库抖动，再根据实际峰值申请用户调整 Docker CPU/RAM或外置依赖。现阶段没有证据要求用户立即换机器。

## 6. 四项重点深化技术与可证伪目标

这些是**工程深化与业务适配**，不是宣称提出全新算法。至少 3 项必须在最终同条件评测中显示优势，否则不能按用户要求作为完成成果交付。目标值用于验收讨论，所有“基线实测/改进实测”当前均为未测。

| 深化方向 | 一般工程基线 | 本项目技术方案 | 预注册主指标与目标 | 配套约束 |
|---|---|---|---|---|
| ① 可验证的复杂计划 | 普通 ReAct + 文字计划，依靠提示词记必需步骤 | 类型化 DAG、前置/后置条件、实体绑定、政策骨架、增量重规划、业务终态验收 | 复杂任务严格成功率至少 **+8 个百分点**；漏必需步骤率相对下降 ≥30% | 同模型/预算；禁止副作用与延迟同时报告 |
| ② 大规模工具准确路由 | 全量工具 schema；另设较强的纯 dense Top-K 基线 | 权限/状态过滤、BM25+dense+RRF、条件 rerank、近义负样本、参数绑定与执行契约 | 对较强检索基线，首次工具+参数联合正确率 **+5 个百分点**；对全量基线 schema Token **−50%** | Recall@K ≥98% 为目标；不能靠删除难工具、拒答提升 |
| ③ 有效期与证据驱动的记忆 | 窗口+普通摘要/纯向量记忆，所有成功聊天都入库 | episodic/semantic/procedural 分层、事实版本、证据引用、观察遮蔽、AWM 晋升回归 | 长会话任务成功率 **+8 个百分点**；输入 Token **−25%** | 已完成副作用丢失、用户纠正遗漏、跨用户泄漏均单独计分 |
| ④ 外部反馈反思与恢复 | 提示词自我批评、固定重试次数；共用底层授权和业务幂等保护 | 业务判定器、错误分型、无进展检测、未知结果对账、租约 fencing、审批绑定版本 | 故障任务正确恢复率 **+15 个百分点**；重复副作用 **0/给定样本** | 安全控制不从基线剥离；零样本事件不等于零风险 |

不把“普通基线”故意做差：必须包括无改进的正常 ReAct、成熟检索 ReAct 和消融实验。达不到目标时先分析、修复和重测，再诚实报告剩余差距；不换模型后仍将数字归因于 Harness。

## 7. 开源复用清单与核验快照

| 项目 | 2026-10-09 核验内容 | 复用范围 | 约束 |
|---|---|---|---|
| LangGraph | 本地 `07b3318`（2026-09-27），包元数据 1.2.12；远端 `bfcfea554ed5` | official checkpoint/store/interrupt、恢复语义、重试机制 | 主干与发行版分开，固定兼容组合；业务副作用仍须幂等 |
| Deep Agents | 远端 `efd88ff8fd12`，GitHub release `deepagents==0.7.23` | 技能按需加载、工具结果卸载、中间件边界 | 不启用子 Agent/任意 shell；MIT |
| LangMem | 远端 `48e3c11f5bb5` | 提取/整合原语，包裹领域校验与审核 | 不是订单数据库；MIT；没有 latest release endpoint 不代表未维护 |
| AWM | `zorazrw/agent-workflow-memory`，远端 `8c0ff8cd11d6`；Apache-2.0 | 轨迹抽取与工作流选择方法 | 原网页任务运行器不直接搬到客服事务 |
| Dify | 本地 `e5b5a1cc`（2026-09-25） | 工具配置、执行可观测与产品交互参照；可部署对照 | 自定义许可证需保留条款/NOTICE；不整体 fork |
| DeerFlow | 本地 `agent-arch/deer-flow` 的 `a723ee6`（2026-09-24） | 上下文卸载、sandbox provider、run ownership/SSE 回放的具体设计 | 通用研究/多 Agent 行为不直接采用；MIT |
| OpenWorker | 本地 `5870585`（2026-09-24） | 工具治理与沙箱/权限边界参考 | aisuite 主执行引擎不替代用户指定 LangGraph；MIT |
| MCP Python SDK | 远端 `91941ed4d398`；release v2.3.0 | 官方传输与类型 | 工具 annotations 是提示而非可信安全保证；MIT |
| AG-UI | 远端 `7d14c417594c`；release/2026-10-08 | 标准事件与 interrupt 协议 | SDK 版本/事件结构需契约测试；MIT |
| Graphiti / Mem0 | Graphiti v0.30.2；Mem0 release 返回 ts-v3.3.1 | 记忆比较与后续可插拔 adapter | JS release 不能当 Python 版本；Apache-2.0；本期不作为默认依赖 |
| τ-bench | `sierra-research/tau2-bench` v1.0.1；远端 `4ce7c0397c1e` | 中文 retail adapter、用户模拟器与状态评分接口 | 当前含 τ³ 知识/语音；MVP 中文文本仅使用合适子集；MIT |
| BFCL | 原作者 `ShishirPatil/gorilla` | 多轮、参数结构、工具相关性判定方法 | 通用 API 准确率不等于电商终态成功率；保留数据许可 |

这里只读源码/元数据；未新增拉取整仓、未安装一整套 Agent 产品。后续复制代码必须记录具体文件、commit、许可证和修改范围；用户接受各类许可证仍不免除条款义务。

## 8. 已发现的设计/脚手架差距

当前目录存在框架和基础设施文件，**尚不是实现完成的 Agent**。本次实际看到 `worker.py` 只做 sleep；后端约束 `langgraph>=0.6,<1`、`langchain-core>=0.3,<1` 与调研代码不匹配；Dockerfile `uv sync --frozen ... || uv sync` 会破坏可复现性；OTel Collector 仅导出 debug；Compose 含旧镜像、开发密码及全网卡端口绑定。

原架构文档中还有：创建 Run 后直接发 Redis 的双写风险、自定义小写事件冒充 AG-UI、所有 AgentState 字段可选、HITL 无审批参数/版本绑定、未区分工具结果信任来源与可信业务字段。本轮设计替换这些假设，代码修复列为下一步，不能将 compose 配置通过等同 Docker 部署通过，也不能将 2 个健康测试等同业务评测通过。

## 9. 一手来源索引

以下均在本轮通过 agent-reach Exa/GitHub CLI/网页后端及网页读取核验。论文用于提出假设，官方文档用于能力与契约，最终收益只能来自本项目实验。

- [S01 Salesforce Hybrid Reasoning / Agent Script](https://architect.salesforce.com/docs/architect/fundamentals/guide/hybrid-reasoning-agentforce-builder-agent-script)
- [S02 LangGraph Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)；[源代码](https://github.com/langchain-ai/langgraph)
- [S03 Deep Agents Context Engineering](https://docs.langchain.com/oss/python/deepagents/context-engineering)；[Deep Agents](https://github.com/langchain-ai/deepagents)
- [S04 LangMem](https://github.com/langchain-ai/langmem)；[Graphiti](https://github.com/getzep/graphiti)；[Mem0](https://github.com/mem0ai/mem0)
- [S05 Agent Workflow Memory 论文](https://arxiv.org/abs/2409.07429)；[作者代码](https://github.com/zorazrw/agent-workflow-memory)
- [S06 The Complexity Trap](https://arxiv.org/abs/2508.21433)；[代码](https://github.com/JetBrains-Research/the-complexity-trap)。软件工程任务里的观察遮蔽结果不能直接迁移成客服收益数字。
- [S07 Reflexion](https://arxiv.org/abs/2303.11366)；[S08 Intrinsic Self-Correction 的局限](https://arxiv.org/abs/2310.01798)。旧模型/任务研究不是对所有新模型的定论，作为反思消融实验的理由。
- [S09 LongMemEval](https://arxiv.org/abs/2410.10813)；时间更新、记忆检索等维度用于设计测试。
- [S10 τ² 论文](https://arxiv.org/abs/2506.07982)；[当前 τ-bench 仓库](https://github.com/sierra-research/tau2-bench)；版本和评分修订必须一起固定。
- [S11 BFCL 作者仓库](https://github.com/ShishirPatil/gorilla/tree/main/berkeley-function-call-leaderboard)
- [S12 MCP SDK](https://github.com/modelcontextprotocol/python-sdk)；[Annotations 的边界](https://blog.modelcontextprotocol.io/posts/2026-03-16-tool-annotations/)；[MCP 安全](https://cheatsheetseries.owasp.org/cheatsheets/MCP_Security_Cheat_Sheet.html)
- [S13 AG-UI Events](https://docs.ag-ui.com/concepts/events)
- [S14 Qwen3 Embedding 0.6B](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B)；[Reranker 0.6B](https://huggingface.co/Qwen/Qwen3-Reranker-0.6B)；[BGE-M3](https://huggingface.co/BAAI/bge-m3)
- [S15 Langfuse 自托管配置](https://langfuse.com/self-hosting/configuration)；[实际 Compose](https://github.com/langfuse/langfuse/blob/main/docker-compose.yml)
- [S16 gVisor 安全模型](https://gvisor.dev/docs/architecture_guide/intro/)；[生产配置](https://gvisor.dev/docs/user_guide/production/)
- [S17 OWASP Prompt Injection Prevention](https://cheatsheetseries.owasp.org/cheatsheets/LLM_Prompt_Injection_Prevention_Cheat_Sheet.html)
- [S18 PostgreSQL 版本支持](https://www.postgresql.org/support/versioning/)；[pgvector](https://github.com/pgvector/pgvector)
- [S19 Elastic RRF](https://www.elastic.co/docs/reference/elasticsearch/rest-apis/reciprocal-rank-fusion)；[订阅能力矩阵](https://www.elastic.co/subscriptions)
- [S20 Redis 许可](https://redis.io/legal/licenses/)
- [S21 智谱 GLM-4-Flash-250414 文档](https://docs.bigmodel.cn/cn/guide/models/free/glm-4-flash-250414)。Exa 返回官方内容；直读发生超时，指定 `glm-4-flash` 别名及能力仍须实际探针。
- [S22 DeerFlow](https://github.com/bytedance/deer-flow)；[Dify](https://github.com/langgenius/dify)；[OpenWorker](https://github.com/andrewyng/openworker)
- [S23 Agent Skills 格式规范](https://github.com/agentskills/agentskills/blob/main/docs/specification.mdx)
- [S24 Jaeger Storage Backends](https://www.jaegertracing.io/docs/2.21/storage/)
- [S25 Ant Design v6 迁移](https://ant.design/docs/react/migration-v6-cn/)；[uv Docker 指南](https://docs.astral.sh/uv/guides/integration/docker/)

## 10. 本轮审查项

本轮请求审查六项：单 Agent 混合执行架构；四项深化与目标指标；ES/本地检索模型分 profile；认证改为共享会话并后置 OIDC；沙箱禁止未验证隔离下的不可信任意代码；先修复步骤 3 再开发核心。现有业务范围、中文、默认模型、React/FastAPI/LangGraph 保留。

未解决问题转为下一步验证任务：模型别名能力/限额、依赖与镜像的发行版兼容、embedding/reranker 的 CPU/GPU 性能、中文分词、AG-UI interrupt SDK 合规、标准 profile 总资源占用。当前不声称这些已经通过。

## 11. 步骤 3 实施后的补充结论

本节更新前文的历史状态。用户确认继续基础设施步骤，并允许在成本合理的前提下选用其他模型；当前默认仍是指定 `glm-4-flash`，云端 embedding/reranker 采用 ADR-020。实际安装版本、镜像 digest、接口探针、故障恢复、备份恢复、观测兼容问题与资源快照见 [交付审查](../delivery/step3-infrastructure-review.md)。代码已落实基础设施，Agent 业务没有提前实现。

百炼两个模型不能共用一个完整请求 URL：向量接口为 `https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings`，精排接口为用户提供的 `https://dashscope.aliyuncs.com/compatible-api/v1/reranks`。同账号密钥已实际验证可用。错误地把 embeddings 放到 compatible-api 路径得到 404，改用官方向量路径后获得两个 1024 维向量；没有用伪造向量替代。

基础可用性已验证，但工具调用探针不是复杂任务能力证明，接口排序例子也不是 RAG 质量基准。AG-UI 已验证 SDK 事件编码，完整中断/SSE 生命周期仍在核心接入阶段验证。中文分词、召回、路由、多步任务质量、模型限额和负载表现仍需后续实验。

补充一手来源：[百炼 OpenAI 兼容向量接口](https://help.aliyun.com/en/model-studio/embedding-interfaces-compatible-with-openai)、[百炼文本精排接口](https://help.aliyun.com/en/model-studio/text-rerank-api)、[OTel 0.162.0 OTLP gRPC exporter](https://github.com/open-telemetry/opentelemetry-collector/blob/v0.162.0/exporter/otlpexporter/README.md)、[Jaeger v3 查询协议](https://github.com/jaegertracing/jaeger-idl/blob/main/proto/api_v3/query_service.proto)。最新版本必须以实际互通为条件：本步发现 Grafana Jaeger 数据源请求旧接口返回 404，移除该失效数据源，以原生 Jaeger 查看已导出的 Trace；Grafana 保留已验证的 Prometheus 数据源与面板。
