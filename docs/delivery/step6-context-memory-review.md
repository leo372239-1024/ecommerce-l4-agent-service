# 步骤 6 交付审查：记忆、RAG、AWM 与上下文工程

2026-10-09。状态：本步首版已开发、部署并验证，用户已授权继续步骤 7，见 [安全交付](step7-security-sandbox-review.md)。以下为步骤 6 历史证据；当前规则会隔离含注入指令的整条知识 chunk，新的正常RAG与恶意资料测试分开执行。实现边界见 [步骤 6 架构](../architecture/step6-context-memory.md)，基础 Agent 和写网关沿用 [步骤 5](step5-business-gateway-review.md)。

## 已实现能力

- 显式偏好、保留期和记忆删除；终态开发任务经事务 Outbox 异步生成去订单标识的历史摘要和向量。
- 认证上传、版本替换、政策审核、持久后台入库和重试；ES BM25 + pgvector + RRF + 百炼精排；授权证据回取。
- 成功开发轨迹的 AWM 候选、结构回放、主管审核、版本失效和 fallback；受控 SKILL.md 导出。
- ContextBuilder 保留关键状态与引用，实际参与模型工具选择；evaluation 分区隔离学习。

上传首版仅支持 UTF-8 TXT/Markdown。知识回答由模型选择相关引用并逐字核验，生成不可用时明确退回检索原文。AWM 回归为结构回放；完整工作台在步骤 8，正式业务/RAG质量基准尚未完成。模型/embedding/reranker为真实接口，商家和支付仍为模拟系统。

## 复现

```powershell
uv run --project backend --frozen python scripts/stack.py up --profile search --profile observability
uv run --project backend --frozen python scripts/verify_context.py --faults
uv run --project backend --frozen python scripts/verify_context_store.py
uv run --project backend --frozen python scripts/verify_business_queries.py --output-dir artifacts/step6
uv run --project backend --frozen python scripts/verify_infrastructure.py --output-dir artifacts/step6
uv run --project backend --frozen python scripts/verify_observability.py --agent-core --output-dir artifacts/step6
uv run --project backend --frozen python scripts/verify_delivery.py --output-dir artifacts/step6
```

--faults 只暂时停止本项目 worker，finally 恢复并保留卷；与其他故障脚本串行。真实验收有少量计费请求。PG probe 使用独立随机 namespace 和确定性 embedding stub，不冒充真实模型验收。报告在忽略目录 artifacts/step6，历史步骤3/4/5证据保留。

后端在 backend 下执行 `uv run --frozen ruff check app tests migrations ../scripts`、`uv run --frozen pytest -q`。锁定 Docker 构建验证前端；无云密钥 CI 的 skip-tool-publish 不算业务验收。

## API 入口

统一使用 agent_session cookie，写请求必须 X-CSRF-Token。

| 资源（/api/v1 前缀） | 操作 |
| --- | --- |
| /knowledge/documents、/knowledge/upload | GET 列表、POST 文本/UTF-8文件入库 |
| /knowledge/documents/{id} | PUT 替换、DELETE；必须 expected_version |
| /knowledge/documents/{id}/approve、/retry | POST 审核或失败入库重试 |
| /knowledge/search、/knowledge/evidence/{chunk_id} | POST query/limit、GET 授权证据 |
| /memories、/memories/{id} | GET 列表、DELETE 单条/全部；全部删除关闭自动记忆 |
| /memories/preferences、/settings | PUT 枚举偏好、automatic_memory_enabled |
| /workflows/candidates | POST 本人成功开发 run 的 source_run_id |
| /workflows、/{id}/review、/{id}/skill | GET 列表、POST validate/approve/disable、GET Skill 导出 |
| /runs | POST 可带 dataset_partition，盲测使用 evaluation |

知识问法：“查询知识库里的质保说明”；订单问法：“查询 ORD-1001 的订单状态”。无有效知识证据时明确转人工，不生成政策结论。

## 验收记录

当前部署 schema 为 0008_evaluation_isolation，后端镜像 l4-agent-backend:stage6。以下为本机验收，非远端 CI、压力测试或正式业务质量基准。

| 验证 | 结果 | artifacts/step6 证据 |
| --- | --- | --- |
| Windows 契约/单元测试 | 86通过，7.57秒；ruff通过 | windows-unit-tests.txt |
| 无网络、只读、非root Linux同源码测试 | 86通过，3.96秒 | linux-unit-tests.txt |
| 真实 GLM/Embedding/Reranker 与部署 API | 13项通过；含 worker 停机后的持久上传恢复 | context-verification.json |
| 独立 namespace 的真实PG与故障适配器 | 8项通过 | context-store-verification.json |
| 原有双查询与补号恢复 | 2项通过，原步骤5报告保留 | business-query-verification.json |
| 当前迁移重跑、SDK、请求限制及备份恢复 | 通过；905727字节备份，1403行checkpoint | infrastructure-verification.json、infrastructure.dump |
| 锁定前端构建 | TypeScript/Vite构建通过 | Docker构建；仍为基础状态页 |
| 当前观测 | Prometheus抓取、API/Worker Trace关联、9类run状态指标、Grafana通过 | observability-verification.json |
| 源码与交付检查 | 3服务各58个源码文件指纹一致，9项本地secret逐值检查，文档链接通过 | source-manifest.json |

13项覆盖：记忆客户/角色隔离、CSRF、入库恢复、真实混合检索/精排、私有证据、政策审核、角色可见性、生效/过期、引用回答、恶意资料不能执行写操作、AWM结构回放与主管审核、本人复用与他人隔离、evaluation分区、真实模型上下文预算、版本漂移fallback、文档替换/删除和异步摘要。单项包含若干相关断言，不能当成13个正式黄金业务用例。

PG回归验证并发唯一领取、已完成段的过期租约恢复、客户/租户过滤、删除与换版期间的迟到embedding、3次失败后显式重试、开启记忆的实际向量写入、全部删除后禁止迟到摘要重建。使用确定性embedding stub，故障测试不宣称实测云服务故障率。

真实知识任务 `86a13008-3aa3-432d-8798-6463f0bf4afc`：SUCCEEDED、2次模型调用、1982个已观察token、0次业务工具调用；引用经当前权限和逐字原文校验。AWM复用任务 `06996871-e3de-45eb-bd24-a5824c460dac` 的关键状态保留，预算上界2123；真实发票工具选择任务 `7553e39b-1066-4204-8bd4-54b70cecce7a` 的上下文上界4983，硬线23040。预算上界是UTF-8保守估计，不是模型实际token，也不能用于宣称压缩收益。

验收发现并修复：HTTP日期/UUID在严格schema下的解析；GLM把知识标题后缀当订单、为固定order目标附加query；模型使用content字段提交引用时的等义规范化（最终仍逐字校验）；历史摘要中同一SQL参数同时绑定text/uuid造成持久化失败。失败run `2923bbc8-d175-4718-b63a-2e36ea3833a8`、`d4aed70b-c04a-4036-ae42-65b5697d0efc` 和生成降级run `622a2414-424d-4e2f-8275-82a32f697d77` 保留，没有改写历史结果。

旧业务回归另外发现：无订单查询被附加未请求的knowledge目标、补号后把发票与物流合并成一个只读节点。现已要求knowledge具有原文通用资料依据，补号继承原目标并限制原文片段，已知跨工具复合查询不能以单一投影报告成功。`98b0af90-8123-4b75-9284-44e8354916c8` 的安全停止和 `bb3d70c3-2ca3-4b8b-9a5d-5207938b6766` 的错误部分完成记录保留；最终2项端到端回归覆盖双查询、补号、三类实际工具和SSE回放。此项修复不证明已覆盖所有开放查询的语义遗漏。

已完成的测试不替代≥50条冻结业务集、RAG召回/回答质量标注、AWM收益消融、生产HA或容量测量。本步没有声称优于Agentforce/Dify或成功率提升。
