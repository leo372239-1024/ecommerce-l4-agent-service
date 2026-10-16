# 步骤 4 交付审查：持久化只读 Agent 核心

日期：2026-10-09。继续沿用已确认的 React/Ant Design、FastAPI、LangGraph、PostgreSQL、Redis、AG-UI 与 OTel 技术栈及冻结依赖。本步完成核心纵向闭环，等待用户审查；不进入步骤 5。

## 可以实际使用的能力

中文任务 → GLM-4-Flash 原生结构化调用 → TaskSpec/每订单目标绑定 → 程序编译及校验 DAG → 五个只读工具 → 确定性验证 → 有界反思/依赖刷新 → 基于证据的中文反馈。订单/物流/售后资格/退款试算可组合，支持不同订单不同目标。缺订单号时暂停，补充后继续；可取消与断点回放。写入请求明确转人工，不宣称退款、改址或取消已执行。

**模型 API 是真实调用，订单和商家政策是明确标识的模拟业务系统。** 当前没有真实商家系统、支付退款通道、MCP 超多工具目录、正式权限角色体系、知识库记忆或完整交互工作台。网页仍为基础控制台，核心通过 API 和审查 CLI 使用；工作台在步骤 8 交付。

## 主要实现

| 模块 | 本步行为 | 主要源码 |
| --- | --- | --- |
| 意图/分解规划 | 原文订单实体校验、每单目标范围、最多两次 schema 修复、强制目标函数调用、程序骨架补足依赖 | `backend/app/agent/contracts.py`、`planner.py` |
| 环境感知 | 每工具独立查归属及当前版本，结果带证据 ID/观测时间/有效期；版本冲突重新读取相关订单 | `backend/app/agent/tools.py` |
| Harness/Loop | 单 Agent StateGraph，节点验证、受控模型反思、白名单修复、无进展停止、活跃时间与调用预算 | `backend/app/agent/runtime.py` |
| Run/恢复调度 | PG 任务与事件事务、SKIP LOCKED、租约/fencing、checkpoint 会话锁、同步持久化、取消/CAS 恢复 | `backend/app/agent/store.py`、`worker.py` |
| 最小身份边界 | 本地随机密码、密码哈希、服务端会话、CSRF、所有任务/事件/工具按真实会话客户隔离 | `backend/app/core/auth.py`、`api/routes/runs.py` |
| 观测 | SDK AG-UI 事件、PG seq/SSE 回放、Redis 提示加 PG 兜底、OTel workflow/tool 关联、持久状态指标 | `core/wakeups.py`、`api/routes/metrics.py` |
| 初始化 | Alembic 0002、SDK setup 单独迁移任务、开发账号与演示数据；幂等初始化 | `migrations/versions/0002_agent_core.py`、`init_schema.py` |

契约与限制详见 [运行架构](../architecture/step4-runtime.md)。检查点不是外部副作用的 exactly-once 保证，真实写操作必须在步骤 5 的业务幂等、审批、版本 CAS 和回执对账保护下接入。

## 实际验收结果

Windows 单元/契约测试 **32 项通过**；同一源码在无网络、只读、非 root Linux 容器内 **32 项通过**。Ruff 检查与格式检查通过。这里的测试计数不等于正式黄金业务用例数量。

Docker 中实际调用 GLM-4-Flash 的最新核心冒烟记录如下。耗时含 CLI 轮询、模型及回放校验，仅是单次样本，不是 P95、SLA 或基线收益：

| 场景 | 预期及实际结果 | 模型调用 | 工具调用 | 已观察 token | 事件数 |
| --- | --- | ---: | ---: | ---: | ---: |
| 单订单状态、物流、资格与金额 | SUCCEEDED；试算 12900 分且未退款 | 2 | 5 | 1240 | 43 |
| A 只试算、B 只查物流 | SUCCEEDED；目标不跨订单扩散 | 2 | 6 | 1250 | 50 |
| 超七天政策拒绝 | SUCCEEDED；有效拒绝结果，无虚构金额 | 1 | 4 | 550 | 36 |
| 查询另一客户订单 | FAILED；不泄露该客户订单事实 | 2 | 1 | 1192 | 16 |
| 请求实际退款写入 | HANDOFF；零业务工具执行 | 1 | 0 | 551 | 8 |

实际发现的模型边界：本次复杂 schema 样本仍有结构修复调用，不能由这些冒烟结果推断 GLM-4-Flash 达到行业最高质量。初版反思请求有普通文本而非 function call 的响应；已实际验证并接入指定函数的 `tool_choice`，无效提议不能进入执行链。模型失败的程序兜底与经验证模型提议分别标记。

核心、安全及故障检查：

1. 无登录返回 401，缺 CSRF 返回 403，另一客户访问 Run/事件返回 404；未知与非本人订单错误相同。
2. 八个并发、相同幂等键的任务创建只产生一个 Run；同键不同消息 409。
3. AG-UI 由 SDK 校验/编码；所有 seq 连续，`Last-Event-ID` 回放只有后续事件，每次已完成 invocation 只有一个 RUN_FINISHED。
4. WAITING_USER 后重启 API 与 worker；两个相同 revision 的并发恢复分别为 202/409，原任务 generation=2 且完成，未重复规划。
5. 暂时失败触发经校验模型反思；第二次相同错误且无新证据停止，工具调用两次，不无限循环。
6. 在已有工具结果/检查点时 SIGKILL worker 后重启，任务继续完成；fencing 1 → 2。最新单次恢复 **48.469 秒**，包含租约等待和专用模拟工具延迟，不能作为恢复 SLA。
7. 两个并发领取者取得不同任务；旧 fencing 无法写事件或终态；PG 会话锁拒绝第二个检查点执行者。
8. 活跃时间预算跨进程保存在 PG；迟到成功结果被取消覆盖；第六条未完成任务被 429 拒绝。
9. Redis 停机时已认证任务继续通过 PG 领取并完成，ready 同时正确返回 503；新登录依赖 Redis 限流而拒绝。
10. 排队取消与执行中取消均有效，停止后不开始下一工具。
11. 执行期间模拟商家将订单版本 1 → 2、实付 12900 → 15900 分；检测旧事实冲突，经模型审查限定刷新依赖后，最终金额及订单证据使用版本 2。
12. Prometheus 真实抓取，八个持久业务状态指标系列；Jaeger v3 中存在 worker run/tool spans 与 workflow_id 关联；Grafana 数据源和三张基础面板可查询。

环境变更测试与 API 重启测试曾被并行启动，客户端出现单实例重启窗口的 502。该测试已改为串行复核，读请求探针支持临时连接错误后的有界重试。单机 Compose 暂不具备 API 故障无中断的多副本高可用能力，仍需后续拓扑与负载/故障测试。

当前十个常驻容器的单次低负载内存快照合计约 **1.65 GiB**，无 OOM；本机资源可支撑当前开发验收。该数字不是压力测试容量，暂不需要调整 Docker 资源。现有 qihai MySQL/MinIO 仍正常运行，KnowDesk/Milvus 未启动或迁移。当前备份约 208 KB，独立数据库恢复成功并验证 507 行检查点。

## 证据与复现

脱敏实测报告在 Git 忽略的 `artifacts/step4/`：

- `agent-core-verification.json`：真实模型、多目标、安全、协议回放、重启与 SIGKILL 验收。
- `resilience-verification.json`：并发领取、旧租约拒绝、预算、取消、限流、Redis 故障。
- `environment-change-verification.json`：版本变化、反思决策及最终金额事实。
- `observability-verification.json`：Prometheus、Jaeger、Grafana 与容器快照。
- `infrastructure-verification.json` / `infrastructure.dump`：当前 schema 重跑与独立数据库备份恢复。
- `source-manifest.json` / `validation-summary.json`：代码/镜像指纹和本步验证摘要。

```powershell
uv run --project backend --frozen python scripts/stack.py up --profile search --profile observability
uv run --project backend --frozen python scripts/verify_agent_core.py --faults
uv run --project backend --frozen python scripts/verify_core_resilience.py
uv run --project backend --frozen python scripts/verify_environment_change.py
uv run --project backend --frozen python scripts/verify_infrastructure.py --output-dir artifacts/step4
uv run --project backend --frozen python scripts/verify_observability.py --agent-core --output-dir artifacts/step4
```

**上述故障脚本串行执行。** 会暂时重启/停止本项目 API、worker 或 Redis，始终保留卷，故障依赖在 finally 中恢复。备份恢复仅使用新建临时数据库。模拟测试订单保留审计，不修改商家演示常规单。模型请求是真实 API 调用，用量取服务端返回；异常时已经发送但未保存的请求可能无法完整计量。

## 亲自审查

无需新增平台注册，本步沿用用户已提供的智谱账号。打开 [API 文档](http://localhost:8000/docs) 可查看全部契约。CLI 从本机 Secret 文件读取演示密码，不在命令行或输出回显。

```powershell
uv run --project backend --frozen python scripts/review_agent.py --stream --message "查询ORD-1001的订单状态、物流、售后资格和预计退款金额"
uv run --project backend --frozen python scripts/review_agent.py --stream --message "只查ORD-1001的预计退款金额，只查ORD-1002的物流"
uv run --project backend --frozen python scripts/review_agent.py --message "帮我查一下订单物流"
```

第三条输出 WAITING_USER 和 workflow_id；使用 `--run-id <该ID> --order-ids ORD-1001 --stream` 恢复。alice 仅能访问 ORD-1001/1002/1003；bob 通过 `--username bob` 访问 ORD-2001。会话有八小时期限；生产模式禁用演示登录，完整身份/租户/RBAC/ABAC 尚待实现。

## 三项深化方向与下一审查门

| 深化方向 | 本步可验收基础 | 后续对照主指标 |
| --- | --- | --- |
| 高质量分解与可证实执行 | 每订单目标 DAG、硬依赖、金额/证据验证、有界修复 | 复杂任务最终完成率、约束违反率、无进展率；B0/B1/P 配对盲测 |
| 实时事实感知与反思纠错 | 外部版本变化触发相关依赖刷新，避免旧事实金额反馈 | 动态环境任务成功率、过期事实使用率、反思净收益与调用成本 |
| 持久、可恢复、可审计执行 | 租约/fencing、同步 checkpoint、持久事件/游标和崩溃接续 | 故障后的正确恢复率、重复副作用率、恢复耗时与吞吐；写幂等在步骤 5 补足 |

这三项是深化实现基础与测量计划，**尚未证明超越 Dify、Agentforce 或普通基线**。正式 ≥50 条黄金测试、超多工具准确路由、RAG/各类记忆/AWM、安全沙箱与 prompt injection 防御、完整工作台和至少三项量化基线收益，仍按后续步骤执行。

用户确认继续后，步骤 5 实现真实业务工具族目录、MCP/Gateway、混合工具路由、写操作幂等/审批/状态机/CAS/不确定回执对账，并逐项故障验收。到该步前，本系统持续保持只读。
