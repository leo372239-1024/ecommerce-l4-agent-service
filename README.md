# 电商 L4 级智能客服 Agent 系统

> **许可：PolyForm Noncommercial License 1.0.0 —— 源码公开，禁止商业使用。**
> 允许个人学习、研究、实验与教学；禁止商业优势、金钱补偿、企业内部商业运营及付费/托管 SaaS。商业授权需另行取得著作权人书面许可。详见 [LICENSE](LICENSE) 与 [NOTICE](NOTICE.md)。本项目**不是** OSI 认可的开源软件。

中文、单Agent的全栈MVP：React/Ant Design工作台，FastAPI服务，LangGraph持久执行，40个商家MCP工具。模型、Embedding与Reranker使用真实API，订单/仓库/财务为可审计的商家模拟系统。覆盖查询→计划→当前事实核验→提案→客户确认/主管审批→事务执行→回执→人工工单闭环。

[面向简历的架构、实现与使用说明](docs/project-architecture-and-user-guide.md)、[交付审查](docs/delivery/step10-final-review.md)、[实际架构](docs/architecture/step10-delivered-system.md)、[运维手册](docs/operations/runbook.md)、[最终量化复核](docs/evaluation/step10-final-v6-quantitative-review.md)。V6确认实验与连续运行验收已完成；结果适用于冻结的模拟场景，详见量化复核。

## 启动

需要Docker Desktop/Compose、uv 0.11.19。镜像digest及依赖已锁定。项目根目录执行：

```powershell
uv run --project backend --frozen python scripts/bootstrap.py
uv run --project backend --frozen python scripts/stack.py up --profile search --profile observability
uv run --project backend --frozen python scripts/stack.py smoke
```

bootstrap生成随机本地Secret并保留已有值。填写`.secrets/openrouter_api_key`及`.secrets/dashscope_api_key`后启动；不向前端或源码写密钥。`--skip-tool-publish`仅检查基础环境，不能接收完整业务任务。

| 入口 | 地址 |
|---|---|
| 客户/客服/主管工作台 | http://localhost:8080 |
| API文档 | http://localhost:8000/docs |
| Grafana | http://localhost:3001/d/agent-infra |
| Prometheus | http://localhost:9090 |
| Jaeger | http://localhost:16686 |

演示用户alice、bob、support、supervisor；密码在本机`.secrets/demo_*_password`，Grafana密码在`.secrets/grafana_admin_password`。宿主端口只绑定127.0.0.1。项目PostgreSQL :5433、Redis :6380，与qihai、KnowDesk、Milvus和其他项目隔离。

## 技术与边界

- 可验证TaskSpec与依赖DAG，实体/参数绑定、预算和有限检查循环。实时感知通过当前商家读取、业务事件与证据版本实现。
- 权限过滤后的混合检索路由及发布方业务投影契约；真实40工具，额外1000描述目录仅用于路由组件测量。
- 拥有者隔离的持久会话、实体账本、上下文预算/引用卸载、语义RAG、情景记忆与审批过的AWM/程序记忆；旧审批及金额不继承。
- 证据来源/时效/版本检查、反思修复、变化状态重新确认、写回执对账、幂等和人工接管。
- HttpOnly会话/CSRF、OIDC代码、RBAC+资源ABAC、提示注入防护、固定工具sandbox、持久AG-UI SSE、OpenTelemetry/Prometheus/Grafana。
- PostgreSQL任务/Outbox/业务事务，Redis Pub/Sub通知与DB轮询兜底；Elasticsearch BM25+pgvector dense+RRF+精排。未部署Kafka/K8s/Istio。

LLM：OpenRouter `deepseek/deepseek-v4.1-flash`，temperature=0.3、reasoning禁用、用户指定provider.only及fallback。兼容环境变量仍名为ZHIPU_API_BASE/ZHIPU_MODEL；按接口host选Secret。百炼`text-embedding-v4`生成1024维向量，`qwen3-rerank`精排。512为示例输出长度，复杂步骤采用公开的分步预算。

知识库支持UTF-8 TXT/Markdown；传输层单文件与整请求分别100MiB，表单边界计入整请求。当前文本解析限制为4,000,003字节、100万字符和1500段，因此本版实际可入库TXT/Markdown约4MB以内，不能将100MiB配置解释为支持100MiB文本入库。PDF/DOCX/OCR及任意生成代码执行未开放。组织IdP、TLS、真实商家、多机数据库HA和异地备份未在本机验收；双进程接管不等于跨故障域HA。

## 验证与测评复现

```powershell
uv run --project backend --frozen ruff check backend/app backend/tests backend/migrations scripts
uv run --project backend --frozen pytest backend/tests -q
uv run --project backend --frozen python scripts/verify_business_flows.py --output-dir artifacts/current
uv run --project backend --frozen python scripts/verify_infrastructure.py --output-dir artifacts/current
uv run --project backend --frozen python scripts/verify_observability.py --business-gateway --output-dir artifacts/current
uv run --project backend --frozen python scripts/verify_delivery.py --output-dir artifacts/current
```

前端在frontend运行`npm ci --ignore-scripts`、`npm test`、`npm run build`。Docker构建冻结依赖并运行测试。CI配置已生成，本机验证不等于远端CI已执行。

数据、schedule、冻结源码、镜像、逐题评分、权威状态、事件与真实调用usage/cost在Git忽略的artifacts/benchmarks。60条EC为开发集；主确认74题含56条新题和18条已知回归，路由50题、长会话50题、恢复58题。重复推理不算独立新样本；评测结果不训练episodic/AWM。

```powershell
uv run --project backend --frozen python scripts/benchmark.py reproduce --manifest artifacts/benchmarks/step10-openrouter-confirmation-v1/source-manifest.json --from-snapshot --check-only
uv run --project backend --frozen python scripts/analyze_step10.py --suite all
```

只有全部日程与完成标记齐全才生成成果分析。重新推理用新的experiment名及`reproduce --from-snapshot --experiment <新名称>`，产生真实费用；外部revision未固定，不保证逐字一致。路由组件与真实业务指标分开。失败批次和旧供应商结果原样保留，见[步骤9历史审查](docs/delivery/step9-benchmark-review.md)。

## 停止与许可

```powershell
uv run --project backend --frozen python scripts/stack.py down --profile search --profile observability
```

保留数据库卷；不执行down -v或全局prune。故障演练在任务/评测空闲时串行执行。Jaeger本地Trace重启丢失，PostgreSQL业务事件持久保留。

项目原创代码采用 **PolyForm Noncommercial License 1.0.0**（非商业性使用，非 OSI 开源许可）；著作权人王庆福（GitHub: [leo372239-1024](https://github.com/leo372239-1024)），Copyright (C) 2026。商业用途需另行取得书面许可。来源及许可边界见[NOTICE](NOTICE.md)。依赖/基础镜像遵循各自许可，Elasticsearch含Elastic License，不宣称全部均为OSI开源许可。
