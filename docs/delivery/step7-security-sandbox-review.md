# 步骤 7 交付审查：安全护栏、OIDC 与 Sandbox

2026-10-09。本步实现与验收完成；范围见 [实现架构](../architecture/step7-security-sandbox.md)。已继续复用 [步骤 6](step6-context-memory-review.md) 的记忆/RAG/AWM、[步骤 5](step5-business-gateway-review.md) 的审批与副作用账本。下一步为步骤 8 React 客户/客服工作台。

## 交付

- 输入 schema、长度、控制字符检查和 Unicode 注入检测；文档/历史参考隔离；MCP 描述与 schema 快照检查、工具内容检查；关键权限、金额、审批和租约仍由业务代码核验。
- 任务、附件、来源名、工单备注、事件/结果和日志脱敏；验证错误不回显原始 input；前端 CSP 与安全响应头。
- OIDC Code + S256 PKCE、浏览器绑定的一次性 state、nonce 与 RS256 签名校验；本地账号绑定；生产配置拒绝不安全状态，演示登录禁用。
- 独立、无网络、无凭据、非 root、只读且资源受限的 broker；三项预置计算工具和真实隔离/停机恢复检查。未开放任意代码或 shell。

## 复现

在 backend 下运行 `uv run --frozen ruff check app tests migrations ../scripts ../sandbox` 与 `uv run --frozen pytest -q`。

在项目根目录运行：

```powershell
uv run --project backend --frozen python scripts/stack.py up --profile search --profile observability
uv run --project backend --frozen python scripts/verify_security.py --faults --output-dir artifacts/step7
python scripts/verify_telemetry_privacy.py --output-dir artifacts/step7
uv run --project backend --frozen python scripts/verify_business_gateway.py --security --output-dir artifacts/step7
uv run --project backend --frozen python scripts/verify_context.py --output-dir artifacts/step7
uv run --project backend --frozen python scripts/verify_context_store.py --output-dir artifacts/step7
uv run --project backend --frozen python scripts/verify_business_queries.py --output-dir artifacts/step7
uv run --project backend --frozen python scripts/verify_business_flows.py --faults --output-dir artifacts/step7
uv run --project backend --frozen python scripts/verify_infrastructure.py --output-dir artifacts/step7
uv run --project backend --frozen python scripts/verify_observability.py --business-gateway --output-dir artifacts/step7
uv run --project backend --frozen python scripts/verify_delivery.py --output-dir artifacts/step7
python scripts/audit_linux.py --output-dir artifacts/step7
```

所有故障脚本串行执行；`verify_security --faults` 仅停止/恢复本项目沙箱，业务故障脚本仅操作本项目 worker/merchant。保留数据卷，不运行全局清理。报告放入忽略目录 artifacts/step7；步骤 3–6 证据不覆盖。模型回归有少量真实计费请求，商家/付款仍为模拟。

## 验证证据

| 验证 | 结果与证据 |
| --- | --- |
| Windows/Linux | 各 222 项通过，包含原有 86 项与 136 项安全测试；`windows-unit-tests.txt`、`linux-unit-tests.txt`。 |
| 输入与参考 | 20 个开发攻击样本的 4 种表示变体共 80 次，覆盖普通、零宽、全角及资料前缀；20 条正常客服请求通过；另外覆盖引用注入、历史/压缩污染、控制字符、嵌套 JSON 脱敏、MCP schema/描述/结果篡改。 |
| 沙箱/API | 7 组真实部署检查，含权限/CSRF、精确金额与大额上界、固定解析、任意执行/伪授权拒绝、恶意文档隔离、实际 egress/根文件拒绝、停机 503/readiness 与重启恢复；`security-verification.json`。 |
| OIDC | 同报告 9 项：生产 demo 禁用、PKCE/签名/安全会话、本地角色绑定、state 重放、会话轮换、未绑定身份/错误 nonce、浏览器绑定、过期 state、CSRF/真实 broker、账号撤销。HTTP IdP 为测试签名 fixture，PG/Redis/沙箱为真实服务。 |
| 业务硬边界 | 10 项通过，含 MCP 独立认证、过期/失效审批、订单/政策变更、账号/主管撤销、过期 fencing、取消及租户移动；`business-security-verification.json`。 |
| RAG/记忆/AWM | 12 项真实接口回归；8 项真实 PG 故障回归，后者 embedding 为确定性 stub；`context-verification.json`、`context-store-verification.json`。 |
| 写入与恢复 | 8 个真实模型任务、9 项业务闭环/故障检查：审批前零写入、退货/主管退款、取消原子性、拒绝、旧报价、工单、响应丢失、提交后崩溃恢复、提交后取消保留事实；`business-flow-verification.json`。 |
| 原有查询 | 2 个真实模型任务，检查发票/轨迹分别路由、缺订单恢复及事件连续性；`business-query-verification.json`。 |
| 基础与交付 | 迁移重跑、readiness、代理/大小限制、SDK/pgvector、备份恢复、Prometheus/OTel/Grafana、源码指纹与本地秘密扫描；对应 infrastructure、observability、source-manifest 报告。 |
| 日志与 trace 隐私 | 使用无真实密钥的 query canary 和指定 traceparent，确认请求 trace 存在且 query/凭据不进入实际 Jaeger 或 API/Nginx 日志，检查 CSP 与 referrer；`telemetry-privacy-verification.json`。 |
| 依赖 | Linux 锁定依赖审计未发现已知漏洞，输出 `linux-dependencies.cdx.json`。显式声明 PyJWT crypto，锁定包版本未变化，总解析图仍为 139 个包。前端 TypeScript/Vite 构建通过。 |

最终计数和报告 SHA-256 汇总见 `artifacts/step7/validation-summary.json`。本步开发期间修复了沙箱健康检查 YAML 换行导致的语法错误、中文紧邻手机号的脱敏边界，以及 multipart ValidationError 可能回显原文的问题；最终版重新部署和回归。一次在根目录启动 pytest 导致包路径错误，随后在 backend 正确目录完成检查，不作为通过记录。

这些是开发回归，不是冻结攻击集或稳健性指标：80 个表示变体不算 80 个独立攻击家族，更不算已完成协议要求的至少 200 个独立攻击/故障案例。未执行远端 CI，未测量质量增益、吞吐/HA 或对第三方产品的领先幅度。规则和脱敏覆盖有限，外部 IdP 生产注册、TLS 入口与实际商家接入仍由部署环境提供。现有开发环境保留演示登录；组织 IdP 适配代码已完成且测试，未冒充真实组织登录验收。

## 下一步

步骤 8 将把这些后端能力接入 React 客户/客服工作台：登录和角色视图、任务提交与列表、计划/步骤进度、AG-UI SSE 断线重放、知识引用、客户确认与主管审批、人工工单接管/对账，以及记忆/知识库/AWM 管理。界面显示业务事实、审批风险和模拟商家标识，避免展示原始思维链、系统提示、密钥或完整个人信息。完成后做浏览器交互验收；步骤 9 再冻结黄金集并进行盲测、对照、消融、压力和恢复评测。
