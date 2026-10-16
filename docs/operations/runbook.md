# 本机部署与运行手册

本机业务为模拟商家，模型、Embedding和Reranker为真实计费API。所有宿主入口绑定127.0.0.1。生产接入真实商家、组织IdP、TLS、多故障域与备份保留需要另行部署验证。

## 启动与登录

在项目根目录执行：

```powershell
uv run --project backend --frozen python scripts/bootstrap.py
uv run --project backend --frozen python scripts/stack.py up --profile search --profile observability
uv run --project backend --frozen python scripts/stack.py smoke
```

bootstrap不覆盖已有Secret。LLM网关密钥填写`.secrets/openrouter_api_key`，百炼密钥填写`.secrets/dashscope_api_key`。界面入口http://localhost:8080；演示客户alice、bob，客服support、主管supervisor，密码分别在本机`.secrets/demo_*_password`。不在文档写入密码。API文档http://localhost:8000/docs。

默认OpenRouter配置仍沿用兼容环境变量ZHIPU_API_BASE/ZHIPU_MODEL；历史`.secrets/zhipu_api_key`仅用于其对应官方接口，不能作为OpenRouter密钥。公共请求配置在[实际实现架构](../architecture/step10-delivered-system.md)说明。工具目录发布依赖搜索profile与模型密钥；skip-tool-publish只供无密钥基础检查，不能当业务交付。

## 健康与故障处理

先检查`/api/v1/health/live`与`/api/v1/health/ready`，再查看Docker健康状态和任务持久事件。Ready失败时停止新任务接纳，保留已有数据和恢复队列。模型错误先检查provider-calls或脱敏Trace、供应商限制及预算，禁止自动无限重试。财务写响应超时先查询operation receipt，不手工再次提交新操作。过期提案应重新读取状态并重新确认；不编辑数据库绕过审批。

Worker停止后租约过期，其他Worker可凭更高fencing接管；30秒量级的接管不等于零停顿。只在同一任务的终态与持久事件上判断完成，不能以HTTP连接关闭判断业务未写入。SSE游标存储在客户端，重连允许重放并去重；发现序号缺口重新同步快照，不跳过未知事件。

本机进程冗余演练：

```powershell
docker compose --project-name l4-agent --env-file .env --env-file infra/images.env -f infra/docker-compose.yml -f infra/ha-compose.yml up -d --no-deps api worker web
uv run --project backend --frozen python scripts/verify_process_redundancy.py
docker compose --project-name l4-agent --env-file .env --env-file infra/images.env -f infra/docker-compose.yml up -d --no-deps --scale api=1 --scale worker=1 api worker web
```

演练会停本项目一个API和一个Worker进程；override不发布8000，使用8080入口。测试需在正常任务空闲时执行，结束恢复基础部署。PostgreSQL/Redis/MCP/web仍单实例，宿主重启会整体中断。

## 验收与备份恢复

```powershell
uv run --project backend --frozen python scripts/verify_business_flows.py --output-dir artifacts/current
uv run --project backend --frozen python scripts/verify_conversations.py
uv run --project backend --frozen python scripts/verify_final_knowledge.py --output-dir artifacts/current
uv run --project backend --frozen python scripts/verify_infrastructure.py --output-dir artifacts/current
uv run --project backend --frozen python scripts/verify_observability.py --business-gateway --output-dir artifacts/current
uv run --project backend --frozen python scripts/verify_security.py --output-dir artifacts/current
uv run --project backend --frozen python scripts/verify_delivery.py --output-dir artifacts/current
```

观测脚本需要同输出目录的business-flow-verification.json，先运行业务脚本并按其CLI指定相同目录。基础设施脚本以pg_dump导出本项目数据库，并在新建临时库恢复校验，保留原库。备份包括客户数据和审计，不公开分享原始dump；本机`artifacts`不是异地备份。上线另设加密备份、保留期、恢复时间目标和定期恢复演练。

verify_final_knowledge.py通过当前网关、Embedding与Reranker验证临时私有文档的入库、引用回答、隔离和版本失效，finally仅删除本次创建的资料；不暂停服务。verify_context.py另提供完整记忆/AWM审核和复用流程验收，`--faults`会暂停Worker，应在正常任务空闲时单独运行。功能验收请求有真实API费用，但不混入质量确认样本。

`--faults`会暂停本项目数据库或Redis，必须在评测和正常任务全部结束后串行运行，finally恢复服务。不要执行全局prune或down -v，不操作qihai、KnowDesk、Milvus或其他项目的卷。现有任务、演示验证和评测业务数据有独立命名空间。

## 升级、监控与容量边界

后端/前端镜像由AGENT_BACKEND_IMAGE/AGENT_WEB_IMAGE选择，API、Worker、MCP与migrate使用同一后端版本。升级不能仅更新三项长期服务而遗留旧migrate容器；旧迁移器不认识新revision时，Compose start依赖检查会阻止恢复。完整stack.py up会同步更新迁移器；手动分步发布先重新创建migrate、确认退出0，再发布长期服务。

先保留源码/锁文件/镜像digest与数据库备份，运行冻结依赖测试，构建新镜像，执行幂等迁移，再发布API/Worker/MCP/web。破坏性schema迁移不得依赖回滚旧容器恢复；本MVP迁移采用前向兼容扩展。现有步骤的评测镜像与源码快照保持独立，不用活动代码覆盖历史证据。

Grafana :3001、Prometheus :9090、Jaeger :16686。Prometheus加载API全不可用、持久指标不可用、过期租约、Outbox积压与持续5xx规则。API进程本地计数可sum，数据库舰队Gauge用max避免重复计算副本。规则在Prometheus的Alerts页面可见；未配置Alertmanager外部通知和值班渠道。阈值是初始运维规则，SLO仍需真实流量窗口和组织值班策略；600秒压力档不是月度可用性承诺。控制API、低事件SSE与真实模型任务指标不能互相替代。

停止并保留卷：

```powershell
uv run --project backend --frozen python scripts/stack.py down --profile search --profile observability
```

本机Secret目录ACL限制当前账户/SYSTEM，容器只读挂载；生产使用部署平台Secret管理。客户会话可删除以停止记忆召回；订单与操作审计、业务留存和知识文件应按部署组织政策另外管理。

## 长会话确认与连续运行

长会话正式推理与离线历史索引使用独立 benchmark 数据库和冻结源码；不可修改正在计分的快照。先检查完成计数和控制进程是否存活，再判断是否仍在运行；旧 interruption 文件本身不表示新一轮已经停止。索引 HTTP400/401不得盲目重试；必须保留已计分前缀与原请求散列，诊断同一请求体后按预登记协议恢复未计分单元。供应商准备阶段失败可能缺用量，需独立对账。

V3/V4 中断后在原实验目录恢复，恢复器从既有 schedule 跳过已评分的 `(case_id, variant, trial)`，只执行缺失键；不要删改 `case-results.jsonl` 或拿新实验目录替代：

```powershell
uv run --project backend --frozen python scripts/resume_memory_confirmation.py --experiment step10-memory-final-v3 --offline-preparation-retry
uv run --project backend --frozen python scripts/resume_memory_confirmation.py --experiment step10-memory-final-v4 --offline-preparation-retry
```

启动前先核对暂停记录中的已评分前缀 SHA-256、实验 schedule 和冻结源码哈希；完成标记 `summary.json` 出现后才启动对应分析器。若恢复因 HTTP400/401 停止，保留错误和前缀，按登记协议诊断同一请求体；不可盲目重放。

HTTP400只允许对记录中 `request_body_sha256` 对应的原请求体发一次诊断。诊断必须在隔离 benchmark 网络/数据库中运行；先用单次探针核对哈希与HTTP状态，只有完全匹配且返回200、时间距失败不足15分钟，才按恢复参数继续。以下以V3为例，V4应替换实验名并使用新输出名：

```powershell
docker compose --project-name l4-agent --env-file .env --env-file infra/images.env -f infra/docker-compose.yml -f infra/benchmark-compose.yml run --rm --no-deps benchmark python /benchmark/probe_failed_offline_embedding.py --experiment step10-memory-final-v3 --output-name exact-failed-body-probe-v3-resume.json
uv run --project backend --frozen python scripts/resume_memory_confirmation.py --resume-after-recovered-http400 --experiment step10-memory-final-v3 --offline-preparation-retry --probe-name exact-failed-body-probe-v3-resume.json
```

哈希不匹配、探针非200、15分钟窗口超时或再次HTTP400时停止该实验，保留评分前缀与失败证据，先审查，不进行盲目重放。探针的usage单独记账，不并入评分任务Token。

最终连续运行使用真实只读任务，输出目录必须新建，不能覆盖已有实验：

```powershell
uv run --project backend --frozen python scripts/verify_live_soak.py --seconds 7200 --interval 60 --with-conversation --output-dir artifacts/current-live-soak
uv run --project backend --frozen python scripts/observe_live_soak.py --directory artifacts/current-live-soak
uv run --project backend --frozen python scripts/summarize_soak_resources.py --directory artifacts/current-live-soak
```

前两个命令分别控制真实任务与非侵入资源采样，采样器可在另一个终端启动。采样器结束后运行第三个命令汇总曲线。输出目录必须是本轮新建路径；V6 的旧目录 `artifacts/step10/live-soak-final-v6` 是仅有两条到达的暂停窗口，不构成连续运行验收，也不能复用。只有控制器实跑达到7200秒、开始与结束部署清单一致、全部到达和失败均保留时才算完成。任务共享同一拥有者会话，交替检查明确订单与历史指代，真实模型调用、预期工具/订单绑定及事件重放均入档；不把每分钟到达节奏称为模型最大吞吐。

正式窗口开始前完成部署源码核对和7项发布检查。若验收要求在窗口内不发起浏览器任务，则在窗口结束后再执行浏览器验收；登录页需要演示用户手动登录，不能从浏览器读取本机演示密码。窗口期间不得升级镜像、重建服务或注入数据库/Redis/Worker故障。冻结评测可在独立 benchmark 容器运行，但共享 Docker 宿主、模型供应商/API 和 PostgreSQL 会影响延迟；`resource-timeseries.jsonl` 只统计普通服务容器，单列 benchmark 容器，不代表空载，也不包含 WSL/Windows 与其他项目资源。报告必须披露这一共享负载边界。

2026-10-10 V6连续运行复核目录为[`live-soak-final-v6-resumed-20261010`](../../artifacts/step10/live-soak-final-v6-resumed-20261010/summary.json)：实际7200.015秒，120/120个只读到达通过，0失败，123次模型调用、298160个报告Token；窗口前后部署清单一致。资源采样223点，采样窗7201.338秒，中位间隔32.436秒、最大间隔34.266秒。普通项目容器同一采样时刻的总内存峰值2.235GiB；采样容器ID未变，末次重启数和OOM均为0。该峰值排除benchmark容器、Windows/WSL及其他项目，且PostgreSQL与benchmark共享；Docker CPU百分比峰值来自不同采样时刻，不可相加为同时负载。旧`live-soak-final-v6`仅两条到达的暂停窗口保留作历史记录，不算完整验收。

2026-10-10 V3冻结源码恢复已完成360/360。恢复审计记录四段计数108→180→275→309→360；HTTP400各仅在对同一请求体SHA-256的探针匹配且返回200后恢复，共3次探针、15416个单独报告Token，不计入评分单元。离线准备共记录6次ReadTimeout重试和3次HTTP400终止；失败调用均无供应商用量/费用回报。原有评分行和schedule保持不变，完整分析见[`step10-analysis-memory-final-v3.json`](../../artifacts/benchmarks/step10-analysis-memory-final-v3.json)。

2026-10-11 V4从暂停前14行继续，启动前逐字节核对case-results、provider-calls和offline-retry-events三个前缀的字节数及SHA-256。进度到154时遇到新的HTTP400；首次诊断在枚举已删除/过期会话时以404终止，未发Embedding请求，记录见[`preflight-probe-v4-not-sent.json`](../../artifacts/benchmarks/step10-memory-final-v4/preflight-probe-v4-not-sent.json)。仅修正诊断脚本的候选会话筛选，排除软删除或过期项；随后对原失败请求体SHA-256作单次探针，匹配且HTTP200，探针报告5156 Token，事件到探针约4分钟。按恢复参数从154条继续；诊断Token与评分Token分开报告，评分器、冻结源码和schedule未修改。

2026-10-11 V6可见浏览器验收以`customer-alice`登录后完成：ORD-1002预计送达/承运商、ORD-1001客服时间/优惠券规则，两项均为SUCCEEDED且证据绑定正确，正式结果见[`browser-acceptance-v6.json`](../../artifacts/step10/final-release-v6/browser-acceptance-v6.json)。第一次只问客服时间与优惠券门槛、未给订单号的尝试发生HANDOFF并创建模拟工单；保留在[`browser-acceptance-scope-failure-v6.json`](../../artifacts/step10/final-release-v6/browser-acceptance-scope-failure-v6.json)，不计入2/2正式验收通过。该负向尝试无商家/财务写操作。
