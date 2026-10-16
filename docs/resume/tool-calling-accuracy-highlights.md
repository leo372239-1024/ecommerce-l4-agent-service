# 简历素材：工具调用准确性维度

> 适用项目：电商 L4 级智能客服 Agent 系统
> 素材来源：`backend/app/business/routing.py`、`catalog.py`、`catalog_view.py`、`intents.py`、`backend/app/agent/planner.py`，以及 `docs/evaluation/step10-final-v6-quantitative-review.md`、`docs/architecture/step9-evaluation.md`
> 整理日期：2026-10-16

---

## 0. 写作策略（先看这条）

**结论：这个维度不要用"准确率 XX%"讲故事，要用"架构约束 + 消融证据"讲故事。**

原因有三条，都是硬约束：

1. **路由规模实验里，项目组与强基线打成平手。** 1000 项目录（40 真实工具 + 960 干扰描述）下，本项目严格成功 150/150，基线 R-hybrid 也是 150/150，差值 **0pp**。基线已达样本上限，实验**无法证明**本项目路由更准。
2. 200 项目录两组同样均 150/150；50 项目录本项目 149/150，基线 150/150，本项目**略低一次**。
3. 成本与延迟本项目反而更差：1000 项目录下本项目输入 283,246 Token、中位 4.10 s；基线 191,260 Token、3.37 s。

因此这条维度的真实含金量在**工程机制**：

- **能不能调**（权限）与**该不该调**（语义匹配）被解耦，且在检索之前就完成裁剪；
- **写操作在架构上不可能通过相似度检索取得授权**；
- 候选空间被四段漏斗从 1000 压到 6，模型的选择难度被人为降低；
- 模型输出的每一个参数都要通过"接地校验"；
- 有一条**消融实验支持**"可验证计划机制显著提升工具调用正确性"。

这些是面试官真正会追问、且你能拿出代码指认的东西。

---

## 1. 可直接粘贴进简历的表述

### 1.1 精简版（3 条，适合项目经历列表）

- 设计**权限优先的工具路由**：身份、角色、订单归属、商家归属在检索前硬过滤，BM25 与 pgvector 两路召回均以工具级 allowlist 作为检索条件，未授权工具的 schema 与描述不进入模型上下文。
- 实现**读写分离的工具准入**：语义检索候选集按发布方契约仅保留只读工具，写操作只能由绑定当前业务版本的提案执行路径触发，从架构上排除"模型被相似描述诱导触发写操作"。
- 构建 **BM25 + pgvector + RRF + 精排的四段召回漏斗**（各召回 30 → RRF Top20 → 精排 Top6），模型仅能在 6 个授权候选内选择或显式转澄清；在 1000 项候选目录（40 真实工具 + 960 干扰描述）、900 个评测单元上严格成功 150/150，候选池放大 25 倍未出现退化。

### 1.2 详版（适合作为项目经历的主 bullet）

> **工具层设计与调用准确性保障**
> - 将工具调用拆为「授权裁剪 → 混合召回 → 融合排序 → 精排收敛 → 参数接地校验 → 受控执行」六段，其中前四段由程序控制、模型只在最后一段做有限选择，把开放式的"从 40 个工具里挑一个"收敛为"在 6 个已授权候选里选一个或转澄清"。
> - 工具目录按**效果（effect）唯一化**建模，40 个 MCP 工具（37 只读 + 3 写）共享统一 Pydantic 参数契约；高频确定性查询走效果精确匹配、**不消耗模型调用**，语义改写才进入混合检索。
> - 引入**接地校验**与**漂移检测**：模型返回的 `order_id` 必须严格等于服务端可信绑定值（`ungrounded_tool_arguments`）、`operation_id` 必须逐字出现在原文、工具名必须在候选集内；每次路由前比对目录 `schema_hash` 与向量元数据，漂移即拒绝执行（`catalog_schema_drift` / `catalog_metadata_drift`），目录未发布则任务入口返回 503。
> - 定位并修复 **MCP SDK 运行时参数校验缺口**：工具 advertised schema 声明 `additionalProperties=false` 未同步到 SDK 运行时参数模型，边界探针发现 37 个只读工具会静默接受多余参数；修复为注册时强制 `strict + extra=forbid` 并 rebuild，40 工具 × 5 类参数注入探针全部被拒绝。

### 1.3 若有空间，补一条消融证据

> 通过消融实验分离"可验证计划"的贡献：关闭类型化 TaskSpec 与校验 DAG 后（A-plan），同一批 36 个新复杂场景的严格成功从 159/180 降至 139/180，family 均衡差 **+8.56pp**，95% 聚类 bootstrap 区间 **[+1.03, +17.59]pp**（不含 0），支持"显式目标覆盖与实体绑定校验提升工具调用正确性"这一判断。

---

## 2. 五个核心亮点与代码证据

### 亮点 A｜权限优先的硬过滤（Permission-first）

**机制：** 商品/订单检索之前，先复核当前身份、角色、订单归属与商家归属，产出一个工具级 allowlist；随后 BM25 检索与向量检索**都以这个 allowlist 作为过滤条件**。

**效果：** 未授权工具不仅不能被选中，连描述都不会进入模型上下文，消除了"模型先看到、再被诱导"的路径。

**代码：**

| 位置 | 内容 |
|---|---|
| `business/routing.py:128-155` | `Router.allowed()`：调用 `MerchantService.context()` 做身份/租户/订单归属复核后，用 `projection_candidates()` 生成 allowlist |
| `business/routing.py:194` | ES 检索 `"filter": [{"terms": {"tool_id": allowed}}]` |
| `business/routing.py:214` | 向量检索 `WHERE tool_id=ANY(:ids)` |
| `routing.py:1` | 模块 docstring：*"model sees only bounded authorized candidates"* |

### 亮点 B｜读写分离，写工具不可被语义检索命中

**机制：** `projection_candidates()` 只保留 `risk == "read"` 的工具。3 个写工具（`create_return` / `submit_refund` / `cancel_order`）**永远不参与相似度检索**，只能由绑定提案的执行路径（客户确认 + 必要时主管审批）触发。

**效果：** 把"模型被骗着改了数据"这一类风险从**提示词层面**下沉到**架构层面**——不是靠 system prompt 说"不要乱写"，而是模型根本拿不到写工具的调用入口。

**代码：**

| 位置 | 内容 |
|---|---|
| `business/catalog_view.py:4-9` | `projection_candidates()`：`if spec.risk == "read"` |
| `business/catalog_view.py:1` | docstring：*"Projection pruning uses publisher contracts, never untrusted tool descriptions"* |
| `business/routing.py:133` | 注释：*"Writes are admitted only by the bound proposal execution path, never generic lookup"* |

### 亮点 C｜四段召回漏斗，把候选从 1000 压到 6

**机制与参数（逐段可查）：**

1. **双路召回各 30**：ES 中文 BM25（`standard + lowercase + cjk_bigram`，不依赖未验证的中文插件）Top30；pgvector `text-embedding-v4` 1024 维余弦 Top30。
2. **RRF 融合 Top20**：`score = Σ 1/(60 + rank)`。
3. **精排 Top6**：`qwen3-rerank` 交叉精排。
4. **受限选择**：模型只能在 6 个候选里选 1，或调用 `clarify` 转人工澄清；工具名不在候选集内 → `tool_selection_requires_clarification`。

**降级设计：** 单路召回失败 → 记 `degraded_single_retriever` 继续；两路全失败 → `tool_retrieval_unavailable` 停止该节点；精排失败 → 降级为 `merged[:6]` 并标 `reranker_degraded=True`。**精排分数未校准，因此不把分数当作写入授权依据。**

**代码：**

| 位置 | 内容 |
|---|---|
| `routing.py:30-35` | `rrf(rankings, limit=20)`，k=60 |
| `routing.py:190` | ES `"size": 30` |
| `routing.py:214` | 向量 `LIMIT 30` |
| `routing.py:242` | `min(6, len(merged))` |
| `routing.py:178-179` | **唯一候选直接返回，跳过模型调用**（`if len(allowed) == 1`） |

### 亮点 D｜参数级接地校验（Grounding）

**机制：** 模型返回的 function call 参数要过四道关：

1. 工具名必须在候选集内，否则要求澄清；
2. 参数须通过对应 Pydantic 模型校验（`ReadArgs` / `ReceiptArgs` / `WriteArgs`，`order_id` 有正则有长度约束），否则 `invalid_tool_arguments`；
3. `order_id` 必须**严格等于**服务端可信绑定值，否则 `ungrounded_tool_arguments`；
4. `operation_id` 若提供，必须**逐字出现在原文**，否则同样拒绝。

**计划层还有一道覆盖校验：** 原文中提到的业务投影，若未被 `queries` 覆盖 → `requested_projection_omitted` 拒绝；订单号用 token 边界匹配，防止 `ORD-100` 误配 `ORD-1000`（`ungrounded_order_id`）。

**代码：**

| 位置 | 内容 |
|---|---|
| `routing.py:352-364` | 工具名、参数模型、`order_id`、`operation_id` 四道校验 |
| `business/catalog.py:12-27` | `ReadArgs` / `ReceiptArgs` / `WriteArgs` 参数契约 |
| `agent/planner.py:96-112` | `ungrounded_query_scope`、`invalid_order_id`、token 边界 `ungrounded_order_id` |
| `agent/planner.py:121-125` | `requested_projection_omitted`：投影覆盖校验 |

### 亮点 E｜效果唯一 + 负向边界描述，堵住"近似但语义不同"

**机制：** 40 个工具每个 `effect` 唯一且等于 `tool_id`。描述里显式声明**不做什么**，例如：

- `get_product_price` —— "查询商品当前售价；**不是订单实付金额**"
- `get_order` —— "查询订单状态与实付金额；**不是支付明细**"
- `quote_refund` —— "试算可退金额和剩余退款余额；**不实际退款**"

**双层应对：** 高频确定性 query 走 `exact_effect()` 精确匹配（共享给规划与路由，`intents.py` 中 31 条高精度投影正则），**不消耗模型调用**；只有未知改写才落入混合检索。含否定/比较语义（"不是/不要/无需"）的 query 强制走语义路由，不用关键词硬猜。

**代码：**

| 位置 | 内容 |
|---|---|
| `business/catalog.py:87-136` | 37 只读 + 3 写工具定义，effect = id |
| `business/catalog.py:82` | `negative_example` 字段 |
| `business/intents.py:8-40` | 31 条 FACETS 高精度投影正则 |
| `business/intents.py:94-99` | `exact_effect()`：单投影且无否定才走精确匹配 |

---

## 3. 实测数据与必须披露的边界

### 3.1 可用数据

| 实验 | 规模 | 结果 | 能否用于简历 |
|---|---|---|---|
| 工具路由（1000 目录） | 40 真实 + 960 干扰；50 例 × 3 trial × 2 组 | 本项目 150/150，基线 150/150，差 **0pp** | ⚠️ 只能说"未退化"，**不能说领先** |
| 工具路由（200 / 50 目录） | 各 50 例 × 3 trial | 200 目录两组 150/150；50 目录本项目 149/150 | ⚠️ 50 目录本项目略低 |
| 规划消融（A-plan） | 36 场景 × 5 trial | 159/180 vs 139/180，**+8.56pp**，CI [+1.03, +17.59] | ✅ 可写，区间不含 0 |
| MCP 运行时校验缺口 | 40 工具 × 5 类参数注入 | 修复前 37 个只读工具静默接受多余参数；修复后全部拒绝 | ✅ 可写，工程故事完整 |
| 后端测试 | — | Windows 与 Docker Linux 各 293 项通过 | ✅ 可写 |

### 3.2 边界（面试被追问时必须能答，简历中不宜省略关键项）

1. **路由实验不支持"更准"的结论**：基线已达 100% 样本上限，差值 0pp。简历若写"路由准确率提升 X%"，属于不实陈述。
2. **960 个是公开描述干扰项，不是 960 个可执行 API**。评测不是官方 BFCL 成绩，真实可执行工具只有 40 个。
3. **本项目路由更贵更慢**：1000 目录下 Token 283,246 vs 191,260，中位延迟 4.10 s vs 3.37 s。
4. **无独立人工盲审**；评测为冻结模拟场景，不能外推为真实商家效果。
5. **不是 100% 可靠**：通用查询仍存在路由失败路径与人工接管（HANDOFF），这些失败样本原样保留。
6. 供应商 serving provider 组成两组不同，成本差异不能全部归因于算法。

---

## 4. 面试追问预案

**Q1：你说"权限优先"，那权限是怎么来的？模型能指定身份吗？**
不能。客户/租户/角色取自服务端会话，模型工具参数里没有身份字段；工作流与 fencing 由 Gateway 放进可信传输头，MCP 服务再回查当前任务与账户。订单租户在创建时独立持久化，账户商家变更不会迁移旧订单。

**Q2：为什么不做纯语义路由，非要加一层精确效果匹配？**
因为工具描述天然高度相似（33 个只读工具里有一堆"查询 XX 状态"）。纯语义路由在近似但业务效果不同的工具上会出错，而且每次都要付费调用模型。我们把 effect 唯一化，高频确定性查询用正则精确匹配直接命中，只有未知改写才进检索。这既提升正确性也省钱。

**Q3：你的路由准确率比基线高多少？**
持平。1000 目录下两组都是 150/150，基线触顶了，这个实验只能证明"候选池放大 25 倍没有退化"，不能证明更准。真正有区间支持的是规划机制的消融：+8.56pp，CI [+1.03, +17.59]。

**Q4：模型选错了工具怎么办？**
三道防线：① 模型只能从 6 个已授权候选里选，选不到就调 `clarify` 转人工；② 返回的工具名、参数都要过校验，`order_id` 必须等于服务端绑定值；③ 写入前还有提案、确认、审批、版本复核，状态变化会让旧提案失效。**把错误拦在"不执行"这一侧，而不是拦在"执行后回滚"这一侧。**

**Q5：最有意思的 bug 是哪个？**
MCP SDK 的运行时参数校验缺口。工具公布的 JSON Schema 写了 `additionalProperties=false`，但 SDK 注册工具时用的运行时参数模型没同步这个约束，边界探针发现 37 个只读工具会静默接受多余的 `tenant_id`。虽然被忽略、未证明越权读写，但违背了公布契约。修复是在注册时同步将 `arg_model` 设为 strict 与 `extra=forbid` 并 rebuild。这提醒了一点：**契约声明与运行时强制是两件事，安全边界必须落在运行时。**

---

## 5. 一句话总结（可直接用于自我介绍）

> 这个项目在工具调用上的核心不是"选得准"，而是**把"选得准"变成一件被结构约束住的事**：模型在被裁剪过的 6 个授权候选里做有限选择，参数逐项接地校验，写操作在架构上不可被检索命中，任何契约漂移都会被拒绝执行；并有一组消融实验支持可验证计划机制对调用正确性的正向效应。
