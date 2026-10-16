# 步骤 7：安全护栏、身份与预置工具沙箱

2026-10-09。实施范围以 [架构 v2](agent-core-v2.md) 第 9 节和 ADR-007 为依据，保留步骤 5 的业务授权与步骤 6 的检索授权。下一步是步骤 8 工作台；正式安全质量评测在步骤 9。

## 信任与授权

会话绑定当前数据库账号、租户和角色。用户文字、文档、附件、历史记忆、AWM、模型输出和沙箱结果不能改变这些值，不能确认审批，也不能直接提交商家写操作。

模型通过原生 function schema 解析意图和选择受控工具；代码核对原文订单绑定、目录快照、金额、依赖版本、租约 fencing 与实际审批。商家每次读写重新查询归属及执行权限。检测规则提供阻断和隔离信号，未命中规则不代表具有操作权限。继续禁止模型安装工具、运行脚本、构造目标 URL 或自行声明主管身份。

| 边界 | 实现与行为 |
| --- | --- |
| 请求与附件 | StrictModel 拒绝未知字段和非法控制字符；任务消息最多 4000 字符。规则先做 NFKC 与零宽字符归一化，高置信度身份伪造、审批绕过、密钥读取及执行命令请求返回 422，尚未创建 Run 或调用模型。 |
| 敏感内容 | 已知密钥格式、凭据字段、手机号、身份证号和邮箱脱敏；结构化地址字段遮蔽。任务、文档、上传来源名、人工工单结案备注在写入前处理；事件、最终结果、证据回取、记忆列表与日志在输出前处理。嵌套 JSON 字符串解析后递归脱敏，保持有效 JSON。 |
| 验证错误 | 全局 422 只返回字段位置、类型与固定提示，不回显 Pydantic 原始 input；multipart 上传也不使用 ValidationError 的原始字符串。 |
| 知识与历史 | 知识 fetch 在 rerank、模型回答、fallback 摘录和引用导出前重新授权并隔离命中注入规则的整条 chunk；ContextBuilder 把所有可选参考统一标成 untrusted_reference，恶意记录整体省略，保留关键计划、事实引用与审批状态。 |
| MCP | 固定内部服务地址、独立服务密钥，逐次核对本地发布描述和完整 schema；结果内容仍经检查、脱敏和业务 Verifier。被篡改的描述/schema 或含指令的结果返回安全错误码。旧只读执行路径与恢复的缓存结果同样在进入事件/checkpoint 前检查。 |
| 出口 | 模型端点、MCP 地址和 OIDC 地址只接受部署配置。沙箱无网络。业务写入仍只走 Gateway；引用和检测结果永不授予权限。 |

默认不增加独立大安全模型或每次额外安全模型调用；复用现有结构化意图解析和确定性业务校验，避免把分类器拒绝/放行当成授权。未来检测器可替换 `signals/reference` 边界，但须保持这些硬性授权条件。当前规则并非通用注入识别器，脱敏也不是覆盖所有语言和个人信息形态的 DLP。

`agent_guardrail_decisions_total{stage,decision}` 只使用有限阶段与决策标签，不记录用户、订单、原文或 token。审批、操作及工单继续使用 PostgreSQL 审计账本。

OTLP 使用 SafeExporter 生成脱敏 span 副本：只保留技术字段、批准路径及业务关联 ID，剔除查询字符串、HTTP headers/body、异常原文/stack 和未知属性；未知路径不输出，URL host 被替换为服务标记。保留 trace/parent/link 关联与有限业务 span 名称，避免 OIDC callback code/state 经默认 HTTP instrumentation 进入 Jaeger。用真实 canary 请求验证 trace 确实产生且敏感查询不存在。

## OIDC 身份接入

新增 migration `0009_security_identity`，readiness 要求此版本。`identity_bindings` 使用固定 issuer、subject 的 SHA-256 和现有账号 ID；账号角色、租户、active 均由本地数据库决定，不采用 token 的 role/email/tenant 自动授权，不提供公共注册或绑定端点。

登录接口：

- `GET /api/v1/auth/oidc/login`：固定授权端点，Code flow，S256 PKCE，随机 state/nonce/verifier，Redis 保存 300 秒，浏览器 HttpOnly/Lax 事务 cookie 单独绑定。
- `GET /api/v1/auth/oidc/callback`：Redis GETDEL 消耗一次性 state，核对浏览器绑定，再通过固定 HTTPS token/JWKS 地址校验签名。仅支持 RS256、RSA 至少 2048 位；核验 iss、aud、azp、exp、iat、nonce、sub，拒绝未知 kid、算法替换、过期/重放及未绑定账号。token/JWKS 响应限制 256 KiB，token 限制 16 KiB，HTTP 5 秒且不跟随重定向，不使用环境代理。
- `GET /api/v1/auth/session`：返回当前角色、租户及 CSRF；不返回 session cookie。新会话撤销同浏览器旧 token，8 小时有效，HttpOnly/Strict，生产必须 Secure。可升级步骤 6 遗留 CSRF 格式。
- `POST /api/v1/auth/logout`：仍验证会话与 CSRF，并撤销服务端会话。

Redis 对 OIDC 启动和回调合计限制同来源 40 次/分钟；Redis 故障拒绝认证。生产禁止演示密码登录，禁止缺失 OIDC 配置、非 HTTPS 端点、非安全 cookie 或非 HTTPS CORS origin。

规范依据：[OIDC Core 3.1.3.7](https://openid.net/specs/openid-connect-core-1_0.html#IDTokenValidation)、[RFC 7636](https://www.rfc-editor.org/rfc/rfc7636)。首次网页阅读链不可达，后以官方原文核对。

部署人员在外部 IdP 注册客户端和精确 callback URL，在 `.env` 配置全部 `OIDC_*` 地址与 client ID、`APP_ENV=production`、`SESSION_SECURE=true`、HTTPS `CORS_ORIGINS`。机密客户端可使用 `infra/production-identity.example.yml` 挂载 `.secrets/oidc_client_secret`，仅 API 接收此 secret，使用 client_secret_basic；公共客户端只用 PKCE。当前仅实现这一签名算法及这两种客户端方式，不宣称兼容所有 IdP 扩展。

账号须由运营通过可信管理流程预先创建，再在 backend 工作目录执行 `uv run --frozen python ../scripts/bind_identity.py --issuer <固定issuer> --account <现有账号ID>`；subject 由隐藏交互输入，不放入命令行或日志。重复/冲突绑定由唯一约束拒绝。生产初始化不生成演示账号。真实组织 IdP 和 TLS 终止配置依赖部署环境，本次使用签名协议 fixture，未注册外部 IdP，也未切换现有开发登录。

## Sandbox

独立 `l4-agent-sandbox:stage7` 镜像仅含 Python 标准库和 broker/runner/health 三个固定文件。API/worker 只读挂载 Unix socket 命名卷；broker 没有 TCP listener、宿主 bind mount、secrets、数据库、Docker socket 或模型 SDK。共享卷只用于 socket，不用于传入可执行文件。

| 限制 | 配置 |
| --- | --- |
| 网络/用户 | network_mode:none，仅 loopback；UID/GID 10001 |
| 权限 | read_only、cap_drop ALL、no-new-privileges、Docker 默认 seccomp |
| 资源 | CPU 0.5、内存 128 MiB、pids 32、tmpfs 8 MiB |
| 协议 | 单次输入至多 64 KiB，输出至多 8 KiB；最多 2 个并行计算，排队 backlog 4 |
| 单任务 | 固定命令启动隔离 Python runner，移除继承环境；wall time 2 秒；子进程 CPU 1 秒、地址空间 64 MiB、文件/核心转储大小 0；API deadline 3 秒 |

`POST /api/v1/sandbox/execute` 验证会话、CSRF 和共享 Redis 30 次/分钟/账号限流，只接受三个预置工具：

- `money_total`：1–200 个非负十进制金额字符串，每项最多 8 位整数、2 位小数，使用 Decimal；拒绝浮点、NaN、指数和额外批准字段。
- `csv_summary`：CSV 最多 32000 字符、200 行、20 列且列宽一致；只返回行列数，不执行单元格公式。
- `text_stats`：最多 4000 字符，只返回字符/行/词数量，字符串中代码永不执行。

所有结果为 `untrusted_computation`、`business_effect:none`，客户端再次校验输出 schema。此工具接口可直接使用；本步不把它发布到商家目录，也不让模型以通用代码节点调用。金额结果不代表订单退款资格、审批或回执。服务失联返回 503，readiness 同样返回 unavailable；恢复 socket 后可重新使用。

普通 Docker 共享宿主内核。任意模型代码、shell 和上传脚本功能保持关闭；开放前需在独立 Linux 节点验证 gVisor/OpenSandbox/microVM 的隔离与 egress。当前隔离证明仅覆盖固定工具容器的实际权限和网络边界。

凭据挂载同时收紧：API/worker 接收 PG、模型和 MCP 必要密钥；迁移容器只接收 PG 与开发 fixture 密码；merchant-mcp 只接收 PG/MCP 密钥；沙箱不接收任何凭据。前端增加 CSP、nosniff 与 no-referrer，API 响应 no-store，不在前端代码嵌入身份/模型凭据。Nginx 不记录访问日志，API 代理错误日志不落盘，防止 callback code/state 被写进原始请求 URL；诊断使用 API 的安全错误码、模板路由指标和 trace。
