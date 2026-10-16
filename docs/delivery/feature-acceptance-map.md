# 需求、实现与验收对应表

2026-10-10。状态以活动源码与本机验收为准；历史设计的候选技术和商业产品能力不自动计为已实现。效果数据另见[真实模型对比](../evaluation/step10-measured-results.md)，部署边界见[实际架构](../architecture/step10-delivered-system.md)。

| 需求 | 已交付实现及代码入口 | 验收证据 / 范围 |
|---|---|---|
| 中文、单Agent、React/Ant Design、FastAPI、LangGraph | [工作台](../../frontend/src/Runs.tsx)、[API](../../backend/app/api/router.py)、[执行图](../../backend/app/agent/runtime.py)；一个认知Agent，API/Worker/MCP属于工程进程 | 真实浏览器同会话连续查询、v6 Windows/Linux Docker均293项后端测试通过及5项前端SSE测试 |
| 复杂拆解与高质量规划 | [TaskSpec与DAG](../../backend/app/agent/contracts.py)、[规划器](../../backend/app/agent/planner.py)；主线由契约编译、子问题模型解析、节点后验证、有限重规划 | 冻结计划确认1110单元；复杂主指标与已知回归分开 |
| 实时感知环境 | [当前商家读取](../../backend/app/business/service.py)、[版本/时效验证](../../backend/app/agent/tools.py)；不将历史观察当实时事实 | 状态变化、观察过期、提案失效、重新确认与恢复专项 |
| MCP与大量工具路由 | [MCP服务](../../backend/app/business/mcp_server.py)、[检索/投影路由](../../backend/app/business/routing.py)；40个真实商家工具，认证后检索与参数绑定 | 900单元的50/200/1000描述目录实验；960个外部描述仅作干扰，不执行外部API |
| Context Engineering / Compaction | [预算构建](../../backend/app/context/builder.py)、[会话实体账本与最近片段](../../backend/app/context/conversation.py)；关键字段保留、历史引用卸载、保守Token上界 | 实际模型输入usage、V1/V2/最终版本长会话专项；各冻结版本与完成状态见测评报告；非生成式摘要、非供应商硬限 |
| Episodic / Semantic Memory | [情景与偏好](../../backend/app/context/memory.py)、[事务Outbox摘要](../../backend/app/context/jobs.py)、[语义检索](../../backend/app/context/retrieval.py) | 历史完整记忆验收、租户/拥有者/删除/时效回归；最终网关知识专项4项通过 |
| Procedural Memory / AWM / Agent Skills | [版本化工作流](../../backend/app/context/workflows.py)；本人成功轨迹→候选→结构回放→主管审核→受控复用→版本失效；可导出受控SKILL.md | 步骤6真实审核/复用验收及契约测试；未实现自动安装第三方Skills，AWM收益未单独量化 |
| Reflection / Self-Critique / Loop | [证据检查与有限修复](../../backend/app/agent/runtime.py)、[受约束critique](../../backend/app/agent/planner.py)；确定性检查优先，模型建议不能扩大权限 | 348单元故障/安全停止/正常对照专项；有次数、Token、工具与活动时间预算 |
| Agent Gateway / Harness | [网关](../../backend/app/business/gateway.py)、[Harness](../../backend/app/agent/runtime.py)、[持久存储](../../backend/app/agent/store.py)；统一授权/提案/回执、checkpoint/租约/fencing | 真实审批与业务闭环9项、响应丢失/提交后停机/取消恢复；不是通用多模型网关产品 |
| Agent Sandbox | [认证计算API](../../backend/app/security/sandbox.py)、[隔离服务](../../sandbox/broker.py)；只读、无网络、CPU/内存/PID限制、固定money/CSV/text工具 | 实际sandbox请求、隔离与资源限制验收；当前为独立受控计算API，未作为主订单DAG节点自动调用，也不执行任意生成代码 |
| 用户过程可观测 / AG-UI | [持久事件](../../backend/app/api/routes/runs.py)、[SSE客户端](../../frontend/src/sse.ts)；标准事件、计划/证据/审批状态、序号重放与缺口同步 | 浏览器截图、SSE测试、低事件连接压力与连续运行；不展示内部思维链 |
| Agent Observability | [OTEL](../../backend/app/core/telemetry.py)、[指标](../../backend/app/core/metrics.py)、[看板](../../infra/grafana/dashboards/infrastructure.json)、[规则](../../infra/prometheus/alerts.yml) | Worker→Gateway→MCP共享Trace、Prometheus抓取、Grafana、规则及promtool测试；Jaeger内存Trace非持久审计 |
| Guardrails / HITL / 直接与间接注入防护 | [信任边界](../../backend/app/security/guards.py)、[审批](../../backend/app/api/routes/approvals.py)、[工单](../../backend/app/api/routes/cases.py) | 真实API安全检查、知识注入隔离、客户确认/主管审批、人工领取/处理/对账/关闭；不宣称所有未知攻击都安全 |
| Agent Evals / Trajectory Evaluation / 黄金集≥50 | [自动oracle](../../backend/app/evaluation/oracle.py)、[冻结/复现工具](../../scripts/benchmark.py)、[确认协议](../evaluation/step10-preregistration.md)；结构、完整参数、当前证据、审批、账务副作用及终态联合评估 | EC60开发集；计划74、路由50、记忆50、恢复58专项；实际2658单元含重复trial；未完成独立人工盲审或开放生成的人类偏好评估 |
| 幂等与消息可靠性 | PostgreSQL唯一约束/状态机/事务/Outbox；[唤醒通知](../../backend/app/core/wakeups.py)用Redis Pub/Sub+DB轮询，Worker凭fencing提交 | 重复任务相同ID、写回执、故障后恰好一次、通知/数据库故障及备份恢复；不以缓存键独立保证资金幂等 |
| 用户认证与授权 | [共享会话](../../backend/app/core/auth.py)、[OIDC](../../backend/app/security/oidc.py)、Casbin RBAC与资源ABAC；HttpOnly/CSRF | 演示账户真实API验收、签名OIDC协议测试；组织IdP/TLS未接入本机，未改为浏览器保存JWT |
| Docker部署、性能与可用性 | [Compose](../../infra/docker-compose.yml)、[同宿主冗余override](../../infra/ha-compose.yml)、[本机手册](../operations/runbook.md) | 控制API600秒压力档、SSE短窗、进程接管、持续真实任务；单机实验不能证明跨区HA、月度SLO或大量模型并发 |

Kafka/RocketMQ、Kubernetes/Helm/Istio、跨故障域数据库、组织IdP/TLS、外部值班通知、真实商家适配、PDF/DOCX/OCR、第三方Skills安装与任意生成代码沙箱均为后续明确需求触发的升级项。本MVP未为尚未成立的规模需求堆叠这些组件。
