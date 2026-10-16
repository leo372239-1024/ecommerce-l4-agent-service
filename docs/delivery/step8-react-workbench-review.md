# 步骤8交付审查：React 客户 / 客服工作台

日期：2026-10-09。工作台已开发并部署到 [本机控制台](http://localhost:8080)，接入现有 API，完成浏览器交互验收。实现与恢复机制见 [步骤8架构](../architecture/step8-react-workbench.md)。

## 本次交付

| 能力 | 已接入行为 |
| --- | --- |
| 登录 | 本地演示登录、共享 Cookie 会话恢复、组织登录配置入口、退出与会话失效清屏 |
| 客户任务 | 提交、缺订单补充、最近任务分页、刷新恢复选中任务、取消 |
| 计划与步骤 | DAG依赖、风险分类、核验进度、公开事件与最终回执 |
| SSE | Last-Event-ID 重放、去重/缺口检查、退避恢复、快照追平 |
| 知识引用 | 检索引用与回答引用抽屉；打开时复查权限、有效期及当前版本 |
| 客户与主管审批 | 订单、金额、版本、有效期审查，客户确认/拒绝与主管批准分离 |
| 人工工单 | 岗位接管、权威对账、最新操作回执、关闭结论、客户可读 |
| 记忆 | 自动摘要开关、明确偏好与保留期、删除/遗忘 |
| 知识库 | TXT/Markdown上传入库、文本创建、范围与角色、替换、重试、政策审核、删除 |
| AWM | 从成功轨迹提取、结构验证、主管批准/停用、Skill预览下载 |

所有页面使用同源 API；生产不展示演示登录，界面按钮不替代后端授权。时间按上海时区、金额以分转换显示。桌面1440×900双栏、390×844单栏均已检查，窄屏分页撑宽问题已修复。

## 已执行验证

Windows 后端 **224项测试通过**，Ruff检查通过；Docker Node24 构建中 **5项SSE协议测试通过**，TypeScript/Vite生产构建通过。同源 HTTP smoke 通过。浏览器22项交互场景已通过。没有运行或声明远端 GitHub CI。

浏览器实际通过三种岗位及第二客户账号操作页面，记录于 `artifacts/step8/browser-acceptance.json`。重点证据如下：

| 场景 | 观察结果 |
| --- | --- |
| 错误密码与角色菜单 | 登录错误可读；客户、客服、主管菜单与权限匹配 |
| 缺订单补充 | run `ecf7b316-3472-4499-a9fe-56030b3c014b` 补充 ORD-1001 后3个步骤核验完成，事件追到#40，重连与刷新无重复 |
| 退货客户确认 | run `0cd19ed9-25e1-4456-9e27-7dd532ce1248` 的模拟订单申请退货，4步核验、事件#52及操作回执可读 |
| 双人退款审批 | run `9955d913-aafb-4770-bcff-bfacdae4272b`，金额¥2499.00；客户确认后商家仍0次写入，主管批准后恰好1次退款 |
| 工单闭环 | case `b4f09913-b72c-4458-af94-983497a6f877` 经客服接管、对账、填写结论、关闭，客户看到同一结论 |
| 知识上传与版本 | TXT入库可检索；资料更新到v2后，历史v1引用打开时拒绝作为当前证据 |
| AWM | workflow `52c24005-3d6d-4d2d-8c81-e259ef16a2e1` 从候选、结构验证、批准到Skill下载，最后停用且导出返回404 |
| 真实上游中断 | API容器停25秒，页面显示自动重连；恢复后回到#52且4步仍只出现一次，脚本finally重启服务 |
| 记忆 | 自动开关关闭/恢复；Bob新建1天简洁回复偏好后删除，原有Alice记忆保留 |
| 客户隔离与会话失效 | Bob打开Alice任务返回404；另一标签页注销后原页轮询发现401并清除私人工作台 |
| 取消任务 | Bob独立任务 `cecb16f6-e52c-48a0-b4ad-0c85fa51aaa9` 取消后终态及事件#7同步 |
| 拒绝退款 | run `29aafaa1-a7e3-4018-a359-d93aaa731ead` 的客户拒绝使提案变为REJECTED、任务CANCELLED，模拟商家仍0次写入 |

9项已执行契约回读记录于 `workbench-contract-verification.json`：匿名登录配置不泄密、角色由服务器解析、记忆权限/CSRF、知识管理能力与隔离、引用再次授权、任务隔离与精确SSE重放、主管前无写入/单次回执/旧revision拒绝、工单与结论授权、停用AWM不可导出。

截图：`browser-desktop.png`、`browser-mobile.png`、`browser-disconnected.png`、`browser-evidence.png`、`browser-refund-receipt.png`、`browser-case-closed.png`、`browser-customer-isolation.png`、`browser-session-expired.png`。测试、构建及smoke日志也保存于该忽略目录。`source-manifest.json`核验Git可纳入文件无Secret值、文档链接有效、三个后端服务源码与本机一致，以及web正在运行最新本地构建镜像、HTTP输出与镜像文件逐字节相符。以上本机证据用于功能审查，不含密码与供应商密钥。

## 复现方式

已有部署可以直接访问8080。演示账号为alice、bob、support、supervisor；密码由bootstrap生成，在本机 `.secrets/demo_*_password` 读取，不能提交到Git。正式环境配置组织IdP后使用组织登录入口。

```powershell
uv run --project backend --frozen python scripts/stack.py up --profile search --profile observability
uv run --project backend --frozen python scripts/stack.py smoke
uv run --project backend --frozen pytest -q backend/tests
uv run --project backend --frozen ruff check backend/app backend/tests backend/migrations scripts
uv run --project backend --frozen python scripts/verify_workbench.py
uv run --project backend --frozen python scripts/verify_delivery.py --output-dir artifacts/step8
```

`verify_workbench.py` 回读本次浏览器报告和固定运行编号，适合在未删本次验收数据的本机重复核验。新的浏览器验收可先运行 `prepare_workbench_acceptance.py` 生成独立模拟订单，随后更新浏览器报告中的运行编号；该脚本会写新订单并替换fixture报告，不能混用旧报告。

可控故障只停止本项目API，不删除卷，并在finally启动：

```powershell
uv run --project backend --frozen python scripts/workbench_disconnect.py --seconds 25
```

先在任务详情点击“重新连接”，故障期间观察自动重连，恢复后核对事件号与步骤。故障脚本不要与其他集成探针并行。前端独立验证在 `frontend/` 执行 `npm ci --ignore-scripts`、`npm test`、`npm run build`；构建镜像也会执行协议测试。

## 实际限制与下一步

浏览器此次使用本地演示身份；组织IdP尚未配置，真实组织跳转/回调验收待配置后执行。OIDC协议及生产隐藏演示登录由后端测试覆盖。商家订单、退货退款为模拟，供应商模型/检索API真实，不能把模拟回执解释为真实收付款。

一次“查询订单状态和物流”的模型任务规范修复预算耗尽，安全转为人工工单；已验收客服接管与处理闭环。另一次明确查询发票和物流节点成功。此现象属于模型稳定性和业务评测关注项，不能用少量浏览器成功案例推算整体成功率。

本步不是步骤9黄金集、盲测、成对基线或压力容量评测；没有效果提升/对标结论。AWM结构验证不代表实际业务收益。前端主包约989 kB（gzip约314 kB），已能运行，后续可按页面拆包。PDF/DOCX/OCR、真实商家与多机HA仍沿用已有阶段边界。
