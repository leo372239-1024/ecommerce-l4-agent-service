# 步骤 5 交付审查：多工具路由、MCP 与业务执行闭环

日期：2026-10-09。状态：步骤5历史交付与证据；用户已授权继续步骤6，当前入口见 [记忆与上下文交付](step6-context-memory-review.md)。实现契约见 [业务架构](../architecture/step5-tools-and-effects.md)，一手来源和选型比较见 [技术证据](../research/2026-10-09-step5-tools.md)。历史步骤4报告保留，不能用其只读边界解释当前代码。

## 本步可以实际执行什么

一个 LangGraph Agent 接收中文任务，生成实体/目标绑定的类型计划，程序补齐并校验依赖，通过受控 Gateway 与独立 MCP 商家服务执行。目录为 **37个只读工具 + 3个写工具**。开放查询先按当前身份/订单/商家过滤，ES 中文 BM25 与 pgvector 召回、RRF融合、qwen3-rerank精排，GLM原生函数从短列表选择并绑定参数；固定业务骨架精确路由。

三类写闭环是：①创建退货申请；②退货经外部仓库验收后，退还整单剩余实付；③取消未发货的已支付订单并退款。均需客户确认，资金操作超过100元还需同商家主管审批。版本、金额、政策、期限、权限或提案 revision 改变时旧授权不能沿用。客户和主管不能互相代替确认。

写操作使用稳定 operation ID、参数hash、业务状态机、实体锁、版本条件和持久回执。商家业务变更/操作回执/Outbox同事务；未知响应先对账。已提交回执不会因缓存TTL失效，恢复按ID精确查询；历史列表默认最多100条。提交后取消明确返回部分完成及原回执。

无法自动完成的任务生成持久人工工单：客户可查；同商家客服可领取、核对、关闭；未确认的副作用禁止关闭。接管包、审计和AG-UI事件持久化。旧 run/fence、撤销的主体权限及账户商家迁移不能获得旧订单写权限。

**模型、Embedding、Reranker接口是真实云端请求；商家订单、仓库及退款渠道是明确标识的模拟系统。** 不是40个真实外部业务系统，也未发生真实支付退款。部分SKU/部分金额退款、改址、换货、补发转人工，不扩大为整单退款。当前阶段不声明已达到L4自主等级或行业顶尖质量。

## 当前实测证据

所有业务集成结果来自已部署的步骤5 Linux Docker镜像，真实SDK MCP调用；夹具使用独立订单/账户命名空间，不修改原有项目容器或卷。下列数字均为本步验收，不是正式黄金集或基线收益。

| 验证 | 最新结果 | 证据文件（忽略目录 artifacts/step5） |
| --- | --- | --- |
| Windows契约/单元测试 | 63通过，9.86秒 | windows-unit-tests.txt、validation-summary.json |
| 无网络、只读、非root Linux容器同源码测试 | 63通过，3.85秒 | linux-unit-tests.txt、validation-summary.json |
| MCP与账本检查 | 8项通过；37个不同只读工具实际调用 | business-gateway-verification.json |
| 中文路由开发样例 | 12/12选对工具，12/12参数正确 | 同上，保留各阶段候选与排名 |
| 规划→路由→MCP端到端查询回归 | 2项通过：双查询与缺订单号补充后恢复 | business-query-verification.json |
| 当前路由样本耗时 | 均值2279.01ms，范围1649.91–5021.07ms | 同上；串行小样本，含云网络，不是P95/SLA |
| 身份/政策/租约安全检查 | 10项通过 | business-security-verification.json |
| 真实模型任务与业务故障闭环 | 8个任务，9项检查通过 | business-flow-verification.json |
| 观测 | 9种任务状态、5种操作状态；8条Gateway/MCP共享trace，90个MCP request spans；关联本轮5个实测工作流 | observability-verification.json |
| Schema/备份恢复/请求限制 | 0006迁移重跑；818958字节备份恢复；1308行checkpoint；API及代理超限413 | infrastructure-verification.json、infrastructure.dump |
| Linux依赖审计 | 未发现已知漏洞；导出SBOM | linux-dependencies.cdx.json |
| 源码/镜像/本地密钥检查 | 三个执行服务源码指纹比对，Git可见文件与本地真实secret逐值比对，文档链接检查 | source-manifest.json |

最后三项检查的“通过”只说明当前检查范围；依赖审计不证明不存在未知漏洞，低负载资源不证明高并发容量。63项单元测试也**不计入≥50条正式业务黄金集**。

端到端回归发现GLM-4-Flash会漏顶层实体字段，或在缺订单号时生成样例编号/空实体查询。仅路由器探针无法发现这些问题。当前实现要求完整任务schema，给出具体白名单修复原因；对完全未绑定的实体/查询等待用户确认，不执行样例编号。补号后重新解析并核验原文片段、原目标和明确补充的实体集合。最终双查询任务为3次模型调用/3次工具调用/3145个已观察token；缺参恢复为6次模型调用/3次工具调用/6170token，分别有32/35条连续事件。它们是开发回归记录，不是质量基线提升或模型最高能力证明。初次失败工作流 `9c67d3e7-bb27-44a9-88ce-6f79d7558322` 的HANDOFF记录保留，未改写为成功。

业务与故障检查具体覆盖：

1. 无客户确认不能派发；客户无法充当主管；另一客户看不到提案/工单。
2. 退货申请→商家仓库验收事实→客户确认→主管审批→12900分退款回执完整闭环。用户宣称“已经验收”不能替代仓库事实。
3. 未发货订单取消/退款同事务；拒绝提案终态CANCELLED，零业务副作用；过期版本确认失效且转持久工单。
4. 五个并发相同操作只产生一次15900分退款、一份回执、一个Outbox和一次版本递增；同ID改变参数被拒绝。两个独立意图争抢4900分余额，仅一个成功。
5. 重复事件不重复消费，环境版本不倒退；101条合成已拒绝历史只用于分页压力，旧成功回执仍可按ID取得，不计为业务成功样本。
6. 退款提交后MCP响应延迟超过客户端超时，查询回执后成功；提交后SIGKILL worker，租约恢复后成功；提交后取消为PARTIAL。三场景均只有一次4900分退款、一份账本、订单版本2。
7. 客服领取工单，存在未知操作时不能关闭；核对后关闭，客户可查CLOSED。核对“未提交”仅适用于本模拟系统的共享事务/fencing契约，远端系统必须提供权威状态查询。
8. MCP独立认证、过期提案、实体/政策变化、读证据政策依赖失效、客户/主管撤权、旧fence、取消任务及商家迁移均被正确处理。

当前11个常驻项目容器低负载内存合计约 **1.872GiB**，无OOM；现有qihai MySQL/MinIO保持运行，KnowDesk/Milvus未启动或迁移。本机当前开发验收无需调整Docker资源；压力容量仍未测量。Jaeger内存存储重启会丢trace，业务事件/账本/工单持久保存在PG。单机PG/Redis/ES和API仍为单实例，不是多机HA。

## 本机审查与复现

启动前 bootstrap 不覆盖本地secret；需要现有GLM和百炼密钥才能发布工具向量。完整profile为本步真实验收配置：

```powershell
uv run --project backend --frozen python scripts/stack.py up --profile search --profile observability
uv run --project backend --frozen python scripts/review_agent.py --stream --message "查询ORD-1001的发票类型和物流轨迹节点"
uv run --project backend --frozen python scripts/review_agent.py --stream --message "为ORD-1001申请整单退货"
```

第二条写任务会停止在WAITING_APPROVAL并打印提案，尚未确认就不会提交；重跑对同一业务状态不一定产生新的可执行提案。审查实际动作、金额、实体/政策版本、有效期后，把真实ID与revision替换下例：

```powershell
uv run --project backend --frozen python scripts/review_approval.py --operation-id <operation_id>
uv run --project backend --frozen python scripts/review_approval.py --operation-id <operation_id> --decision confirm --expected-revision <已审查的revision>
uv run --project backend --frozen python scripts/review_agent.py --run-id <workflow_id> --stream
```

需要主管时，客户确认后由 `review_approval.py --username supervisor --operation-id <ID>` 重新读取最新revision，再显式 `--decision approve --expected-revision <最新revision>`。CLI读取本机随机密码、不回显；身份/CSRF由服务端校验。额度以下只需客户确认。不要把例子中占位符原样运行。

API文档：`http://localhost:8000/docs`。新增资源为 `/api/v1/approvals`、`/operations/{id}`、`/cases` 及工单claim/reconcile/close。客户alice/bob、客服support、主管supervisor采用不同演示会话。生产环境禁用demo登录，正式IdP在步骤7接入。现有网页 `http://localhost:8080` 仍为基础控制台，完整审批/客服工作台在步骤8。

串行执行集成检查，避免故障窗口影响其他探针：

```powershell
uv run --project backend --frozen python scripts/verify_business_gateway.py
uv run --project backend --frozen python scripts/verify_business_gateway.py --security
uv run --project backend --frozen python scripts/verify_business_queries.py
uv run --project backend --frozen python scripts/verify_business_flows.py --faults
uv run --project backend --frozen python scripts/verify_infrastructure.py --output-dir artifacts/step5
uv run --project backend --frozen python scripts/verify_observability.py --business-gateway --output-dir artifacts/step5
uv run --project backend --frozen python scripts/audit_linux.py --output-dir artifacts/step5
uv run --project backend --frozen python scripts/verify_delivery.py
```

故障检查暂时杀死/重启本项目worker，保留卷且 finally 启动本项目服务。路由及业务探针有少量真实云模型计费；不会调用真实商家或真实资金通道。基础CI可 `stack.py up --skip-tool-publish` 无云密钥启动依赖，但未发布目录拒绝新Agent任务503；该模式不算业务验收。代码检查在backend运行 `uv run --frozen ruff check app tests migrations ../scripts`、`uv run --frozen ruff format --check app tests migrations ../scripts`、`uv run --frozen pytest -q -p no:cacheprovider`；锁文件检查 `uv lock --check --offline`。远端GitHub CI尚未执行。

## 三项深化与正式成果门

| 深化实现 | 正式基线 | 改进实现 | 后续必须测量 |
| --- | --- | --- | --- |
| 可验证规划/环境感知 | 同保护的普通ReAct/检索ReAct，静态任务上下文 | 实体目标DAG、必需依赖、政策/实体证据版本、局部刷新、有界反思 | 复杂任务成功率、计划覆盖、环境变化恢复率、调用/成本及消融 |
| 大工具目录准确调用 | 全量描述或普通语义检索ReAct | 权限先过滤、词法/dense/RRF、精排Top6、schema/参数/效果绑定 | Recall@K、工具/参数正确率、误写率、50/200/1000目录档、成本/延迟 |
| 可恢复业务副作用 | 同鉴权/幂等底线的成熟通用执行方案 | 持久意图与批准、当前权限/版本锁、稳定ID、回执对账、未知效果人工闭环 | 有效完成率、重复副作用/未知状态、故障恢复、审计完整性与消融 |

**上述基线指标当前没有实测，不能填写提升百分比。** 按 [成对评测协议](../evaluation/benchmark-protocol.md) 在步骤9冻结≥50条可执行黄金集、真实Docker运行B0/B1/P与消融、报告样本/成本/统计区间和失败案例。80条候选规格尚不是可执行黄金集。达到项目质量目标取决于正式测评通过，不能用本阶段12条开发样例推断超越Agentforce/Dify。

审查通过后的下一步为**步骤6：记忆、RAG知识库、AWM与上下文工程**；步骤7完成护栏/沙箱/生产身份，步骤8完整React工作台，步骤9正式业务与基线评测，步骤10综合部署交付。本步骤完成后停下，等待用户审查及“继续”。
