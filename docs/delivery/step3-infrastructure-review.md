# 步骤 3：基础工程交付审查

日期：2026-10-09。状态：本步骤实施和本机验收完成，等待用户审查；尚未进入步骤 4。范围为发行版兼容、部署、迁移、配置边界、健康检查和观测。下述结果来自真实 Docker Desktop/WSL2 及真实模型 API，**不是 Agent 业务基准结果，也不是生产高可用认证**。

## 1. 实际采用的组件

| 组件 | 锁定/实际版本 | 本步用途 |
| --- | --- | --- |
| Python / uv | 3.12.13 / 0.11.19 | host/container 一致解释器；uv.lock 冻结安装 |
| FastAPI / Starlette | 0.143.0 / 1.7.0 | 原生请求大小限制、原生 OTel tracing、健康 API |
| React / Ant Design | 19.3.0 / 6.6.5 | 实时基础服务状态页 |
| Vite / TypeScript / Node | 8.3.4 / 5.9.3 / 24.15.0 | 锁定前端生产构建 |
| LangGraph / core | 1.2.14 / langchain-core 1.6.9 | SDK 与持久化恢复兼容性探针，尚无业务 Agent |
| 官方 PG Checkpointer | 3.1.2 | 断开连接后用新图实例恢复状态 |
| MCP / AG-UI | 2.3.0 / 0.1.22 | 官方 SDK；AG-UI 事件编码契约；工具协议服务与完整 SSE/HITL 后续接入 |
| PostgreSQL / pgvector 扩展 | 18.6 / 0.8.7 | Alembic、Worker 心跳、检查点与真实向量运算 |
| SQLAlchemy / asyncpg | 2.1.4 / 0.32.0 | 有界异步连接池、pre-ping、依赖超时 |
| Redis | 8.10.2 | AOF、Worker TTL 心跳；任务 Streams/Outbox 未实现 |
| Elasticsearch / Python client | 9.5.5 / 9.5.1 | search profile，真实集群就绪检查；领域分词与 BM25 尚未实现 |
| OTel SDK / Collector | 1.45.1 / 0.162.0 | API 原生 instrumentation → OTLP HTTP → Collector → OTLP gRPC |
| Jaeger / Prometheus / Grafana | 2.22.0 / 3.15.0 / 13.2.3 | 真实 Trace 查询、指标抓取、预置面板 |

10 个基础镜像已按 [images.env](../../infra/images.env) 固定不可变 digest；应用构建引用这些 digest。SDK 安装版本是发行包实测组合，区别于调研时的 GitHub 主干快照。保留 `uv.lock`、`package-lock.json` 与构建上下文；禁止 frozen 失败后改成浮动安装。当前镜像中的 18 个 app Python 文件已与本地源码逐文件 SHA-256 比对一致。

## 2. 本机实测结果

| 验收项 | 实际结果 | 可复现证据 |
| --- | --- | --- |
| 后端规则/边界/适配器测试 | 14 passed | `backend/tests/`；包括无 Content-Length 的大小限制、失败 readiness、敏感异常脱敏 |
| 静态检查、锁文件 | Ruff 与 uv lock --check 通过 | [CI 配置](../../.github/workflows/ci.yml)、锁文件 |
| 前端生产构建 | TypeScript + Vite 通过；Docker 内 npm audit 0 vulnerabilities | [前端 Dockerfile](../../frontend/Dockerfile)；单 chunk 585.63 kB，分包优化留到完整工作台步骤 |
| Python 依赖审计 | host 与 Linux 构建容器未发现已知漏洞 | [host 审计](../../artifacts/step3/python-audit.json)、[Linux SBOM](../../artifacts/step3/linux-dependencies.cdx.json)，后者 134 个组件、0 条已知漏洞 |
| 全部 profiles | 10 个长驻容器运行，migrate 正常退出 0 | Compose 与 [观测验收](../../artifacts/step3/observability-verification.json) |
| HTTP / 同源代理 | API readiness、代理 readiness、web health 200 | [基础验收](../../artifacts/step3/infrastructure-verification.json) |
| 过大请求 | API :8000 与 Nginx :8080 均在声明超限时返回 413 | 100 MiB + 1 的 Content-Length；实际流式边界另以缩小阈值做单测 |
| 检查点恢复 | 首图中断 → 连接关闭 → 新连接、新图恢复 count=1 | [runtime_probe](../../scripts/runtime_probe.py)，真实 PostgreSQL 持久化 |
| pgvector | 实际向量余弦距离 SQL 运算通过 | runtime probe，非空配置占位 |
| 迁移再次执行 | revision 保持 `0001_infrastructure` | Alembic 再运行与数据库查询 |
| 备份恢复 | 10001 字节逻辑备份恢复到独立临时库；9 行合成 checkpoint、revision、vector 运算校验通过 | [基础验收](../../artifacts/step3/infrastructure-verification.json)、`artifacts/step3/infrastructure.dump` |
| Redis 停机 | readiness=503，liveness=200；重启后 readiness=200 | 单次 restart→ready 1.031 秒 |
| PostgreSQL 停机 | readiness=503，liveness=200；重启后 readiness=200 | 单次 restart→ready 2.031 秒 |
| Prometheus | `up{job="agent-api"}=1`，查询到 4 条请求计数 series | [观测验收](../../artifacts/step3/observability-verification.json) |
| Jaeger | v3 查询到 agent-api 的 5 条抽样 Trace | 同上，实际接收链路通过 |
| Grafana | 数据源 health OK、面板 UID agent-infra、3 个 panel | 同上；请求率、P95、抓取可用性 |
| 浏览器 | 实际页面显示数据库、调度存储、搜索正常；业务功能建设中 | 已在 Codex 浏览器打开 http://localhost:8080，核对页面运行状态 |
| Secret | 非 root API 容器读取指定 Secret 并完成三个外部 API 探针；host ACL 已收紧 | [模型探针](../../artifacts/step3/provider-probe.json)，输出无密钥/原始响应正文 |

依赖“未发现已知漏洞”只表示当次 advisory 数据库与扫描范围，不代表镜像/业务零漏洞。本步没有做完整 OS 镜像 CVE 扫描、渗透测试或全业务安全评审。GitHub CI 文件已配置，本地对应检查已执行；未推送仓库、未声称远端 Actions 成功。

审计曾检出原测试依赖 pytest 8.4.2 的 PYSEC-2026-1845，已升级并锁定 pytest 9.1.1，14 项测试与后续审计通过。脱敏实测文件、锁文件和本步源代码/镜像标识可由 [source-manifest.json](../../artifacts/step3/source-manifest.json) 对照；原始材料均保存在本机，不对外上传。

恢复秒数是本机干净停机/重启的**单次基础设施观测值**，从发起 start 到 readiness 恢复；轮询粒度 1 秒，不能当成高可用 SLO、Agent 任务恢复率或并发性能。临时恢复库只在本项目数据库实例创建，验证后删除精确的新库名；原实例数据和其他项目卷未删除。

## 3. 模型接口与账号

三项探针在最终非 root API 容器内运行，采样时间 2026-10-09T02:26:47 UTC：

| 类型 | 模型与地址 | 实际探针 |
| --- | --- | --- |
| LLM | glm-4-flash；智谱 `/api/paas/v4/chat/completions` | 返回 query_order，JSON 参数准确为 TEST-001；该次 159 input + 13 output = 172 tokens；没有执行订单查询 |
| Embedding | text-embedding-v4；百炼 `/compatible-mode/v1/embeddings` | 两段中文，两个 1024 维向量，元素类型/有限性/维度/索引/数量校验 |
| Reranker | qwen3-rerank；百炼 `/compatible-api/v1/reranks` | 三候选、top_n=2，响应索引/数量/分数校验；请求不含 dimensions |

已经持有的百炼、智谱密钥均可用，当前无需新增注册。若需以后创建其他模型账号，先给出官方注册入口与成本/质量对照，再切换。现模型探针只证明连通与最小契约，不证明复杂计划、多工具路由、中文检索质量或账号生产限额。

用户允许合理成本下选用其他模型。实施策略是先固定现模型开发/评测，开发集暴露能力瓶颈时再比较候选强模型；主基线与优化方案用相同模型，模型更换作为独立实验。使用量、请求次数、token 和冷/热请求在正式评测记录，费用以各平台实际账单和冻结时价格表核对。

一手契约：[百炼向量接口](https://help.aliyun.com/en/model-studio/embedding-interfaces-compatible-with-openai)、[精排接口](https://help.aliyun.com/en/model-studio/text-rerank-api)。首次向量探针错误使用 compatible-api 路径得到 404，已修正为 compatible-mode 路径并在容器验证；未伪造向量。

## 4. 稳定性、安全与兼容处理

应用服务以 UID 10001 运行、只读根文件系统、限额 tmpfs、cap_drop ALL、no-new-privileges、优雅退出；Nginx 使用非特权镜像。无 Docker socket、宿主开发目录或全局卷挂载进应用；只挂载该服务所需 Secret。数据库/Redis/观测镜像遵循各自启动所需权限，不能宣称全部服务均只读或均无 root 初始化。

Windows Secret ACL 只保留当前用户、SYSTEM；Linux 用 0700 私有目录、0644 文件和服务级只读文件挂载兼容 non-root。Compose 文件型 Secret 的 uid/gid/mode 重映射能力有限，依据 [Docker 官方说明](https://docs.docker.com/reference/compose-file/services/#secrets)；本地文件型 Secret 不是生产 Secret Manager。应用 JSON 日志脱敏敏感字段与常见密钥格式，不记录 provider 响应正文、认证头；HTTP 访问路径/查询日志关闭，流量由指标计数。此设置不是对任意业务 PII 的完整防护承诺。

readiness 同时检查数据库 schema revision、Redis 和启用的 ES，设置有界超时；依赖不可用返回 503，进程活性独立返回 200。连接池有界、连接 pre-ping，Redis AOF + noeviction。Worker 每 5 秒写真实 PG/Redis 心跳，Redis TTL 20 秒，SIGTERM 退出清理；**该 Worker 尚不消费任务**，不存在用 sleep 冒充 Agent 完成状态。

修正了 Compose tmpfs 列表中逗号未加引号的部署错误。FastAPI 当前版本原生 tracing 能保留完整路由前缀；指标标签从公开 OpenAPI 模板生成，避免原始资源 ID 造成高基数。OTel exporter 改用当前 `otlp_grpc` 名称，链路不止导出 debug。

实际发现 Jaeger v2.22 查询 v3 正常、旧 `/api/services`/`/api/traces` 返回 404，Grafana Jaeger 数据源 health 失败。因此移除失效数据源：Grafana 展示已验证的 Prometheus 指标，面板链接打开 Jaeger 原生 Trace UI。没有把容器 running 当成数据源互通。Jaeger 为内存后端，重启丢失 Trace；PG 业务审计事件在后续步骤持久化，正式生产 Trace 持久化列为升级项。

## 5. 资源与部署边界

机器实际物理内存约 15.7 GiB，Docker 可用约 9.7 GiB、4 CPU；GPU 不参与当前云端模型推理。本步完整 core + search + observability 的一次空载 `docker stats` 快照合计 **1587.95 MiB ≈ 1.55 GiB**，无 OOM，检查时没有自动重启。ES 约 0.93 GiB；Grafana 默认限额从 256 MiB 提升到 384 MiB，避免把其当前正常运行占用压在边界附近。

空载 CPU/RAM 各容器原始数据见 [观测验收 JSON](../../artifacts/step3/observability-verification.json)。目前资源足以继续本地开发；未进行业务并发压测，不能从空载推导最终峰值容量。云模型 RPM/TPM、并发上传、文档解析和检索建索引仍会影响容量，正式 Docker 压测时报告瓶颈与扩容建议。

Compose 是单机验证拓扑，PostgreSQL/Redis/宿主仍是单点。本步骤不能交付跨可用区 HA；架构保留后续副本、故障转移与横向扩容接口。qihai MySQL/Minio 的容器和数据卷不改动；原停机的 KnowDesk/Milvus 未被启动或迁移。

## 6. 本步未交付的核心内容

未实现客服身份与授权、Agent 计划 DAG、业务工具与 MCP 服务、订单/退款 Mock 事务、操作账本/Outbox、任务消费/租约、HITL、AG-UI SSE、知识库上传解析检索、记忆/AWM、沙箱执行及 Prompt Injection 全链路。已安装相应 SDK 不等于这些功能已经存在。

上传边界落实到入口/ASGI/可复用文件流：单文件及整请求各 104857600 字节。multipart 额外内容计入总量，因此恰好 100 MiB 文件会超出 100 MiB 的完整请求。上传业务尚未接入；并发入库、磁盘预算、解析隔离、对象存储配额将在知识库步骤一并实施。

[80 条候选用例](../evaluation/golden-case-catalog.md) 尚不是可执行黄金集。四项深化和主基线 B0/B1/完整方案/消融的 [评测协议](../evaluation/benchmark-protocol.md) 保留不变。**至少三项深化的真实量化收益尚未测得**，不存在借此次健康检查、公开论文分数或模型切换声称超越 Dify/Agentforce 的结果。

## 7. 复现与下一审查门

项目根目录启动/检查/停止命令见 [README](../../README.md)。诊断证据写入 `artifacts/step3/`，Git 忽略；源码、锁文件和设计文档保留在仓库。当前未创建提交或对外发布。

步骤 3 审查通过后才开始步骤 4：同一模型的深入能力探针、Run 状态机及 checkpoint、TaskSpec/DAG、确定性 Verifier 与有界 Loop、最小工具和事件联调。后续每个步骤继续按用户要求停下审查。业务效果必须在完整系统 Docker 部署和冻结黄金集上实测，不足则改进后重测，不预先宣称达到行业顶尖。
