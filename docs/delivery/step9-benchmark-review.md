# 步骤9交付审查：探索性评测与容量边界

执行日期：2026-10-09至2026-10-10。本轮已完整结束，保留负向结果；修复后的OpenRouter确认实验另见[步骤10](step10-final-review.md)，不同模型/源码不混算。协议成果门与工程执行完成分别判断。

## 实现与证据

- 60条静态EC案例、每条5次、七组共2100个episode，真实deepseek-flash（non-thinking）与真实Docker业务工具；模拟商家账务。见[执行登记](../evaluation/step9-preregistration.md)和[实现架构](../architecture/step9-evaluation.md)。
- 使用独立业务库、MCP与API :8009；原演示库、:8000工作台和服务保留。归档目录`artifacts/benchmarks/step9-deepseek-main/`含源码快照/hash、环境、日程、逐题终态、权威前后状态、工具事件、供应商用量和资源时间序列。
- Windows与隔离Linux各244项后端测试通过；ruff通过。无新增依赖，uv.lock保持冻结。原80条GC规格未声称已全部转为黄金集。
- 作废批次保留：v1脚本客户批准范围过宽；v2完成283条后控制器初始化COMMIT超时。v3完成841条时用户指定切换DeepSeek，主动停止并保留。所有变体统一用v4从头运行，不合并历史GLM数据、不选择成功重测。v3增加证据业务来源绑定、运输错误归档、控制器初始化独立连接，并统一并发4。

## 比较结果

完整2100条结果：P为236/300（78.67%），B1强dense检索为257/300（85.67%），B0全目录ReAct为279/300（93.00%）。族平衡P−B1为−7.50百分点，聚类95%区间[−22.14,+7.50]；P−B0为−15.36百分点，区间[−27.86,−2.86]。本轮没有证明改进效果，不能从其他批次挑选成功结果替代。300次是60题各5次，不是300条独立新题。

所有组自动权威状态/授权/账务护栏没有检出安全失败；这不涵盖最终自然语言沟通。A-plan成功231/300，A-route238/300，A-memory236/300，A-reflect235/300；这些静态用例上的消融未形成足够的深化收益证据。原始summary、trajectory和provider-calls均保留。

GLM v3运行中发现EC011评分瑕疵：共同安全过滤器把phone遮蔽为`[REDACTED]`，旧答案要求保留掩码手机号。切换模型的新v4批次开始前统一修正；60题均重新执行，原GLM错误口径不混入新表。其余任务文字/状态/客户脚本不变，供应商兼容修订和阶段限制见执行登记。

## 容量实测

来源`artifacts/benchmarks/step9-capacity/capacity.json`。控制API查询`/runs?limit=20`不调用模型；资源与正常项目共享宿主。以下只能说明该次开发部署观测，不能称生产稳定容量。

| 场景 | 实际时长/请求 | 结果 | 解释 |
|---|---|---|---|
| API 1并发 | 600秒、37072请求 | 61.79 RPS；P95 31ms；0错误 | 完成本档 |
| API 10并发 | 120.281秒、13176请求 | 109.54 RPS；P95 109ms；52次502，0.395% | 最近100请求延迟超过预设阈值，早停；不是完整600秒稳定档 |
| API 50/100并发 | 未运行 | 无结果 | 依早停规则不升档 |
| SSE 10/50/100连接 | 每档60秒 | 未观察连接错误 | 排队观察流，每流仅少量事件；不是高事件吞吐或推理负载 |
| SSE 200连接 | 60秒 | 6次HTTPStatusError、1次RemoteProtocolError | 本档失败；不能宣称支持200并发 |
| 他人SSE | 单次隔离检查 | 404 | 此探针通过，不能替代全面权限验收 |

API 502的根因未得到链路级证据，不归因于数据库池或供应商。首次容量驱动仅阶段采样资源，后续脚本增加10秒采样/内存90%早停；不能把新增规则追溯为初次运行条件。Docker CPU百分比按核计，不能直接解释为宿主整体CPU利用率。

## 安全与恢复

MCP运行时多余参数拒绝已修复：advertised JSON schema与SDK参数模型原先不一致，37个只读工具会忽略多余tenant_id。该输入未改变可信身份，未观察到账务变化。修复后40工具×5类型共200次实际MCP探针全部拒绝，订单不变、成功业务操作0；前后结果均保留。这是五类强相关边界探针，不能称200个独立模型攻击案例，也不能由0/200证明普遍安全。

本轮持续负载实际7201.407秒，有208个真实只读任务到达并调用模型，另有主批2100任务；不是等待空闲两小时。恢复与资源原始记录保留，当前容量复测另见步骤10。该持续验证使用旧官方DeepSeek与旧源码，不能追溯为新OpenRouter版本的两小时业务证明。历史工作台物流通过workflow `6b1be8fb-ed28-4657-a19c-5ea69eb38a12`。兼容ZHIPU_*文件名保留，但现在按provider host选择对应Secret。

## 完整成果门

已确认的答复问题：P的成功退款最终答复仍拼接旧报价“需退货验收；尚未提交退款”，随后又写“退款已执行”。该条订单本已验收且真实账本/回执正确，但文本与最终状态不一致。例：workflow `961f42dc-cf1d-43c2-8014-4c8ee4b5e734`，轨迹在主批次`trajectory.jsonl`。这说明自动副作用评分不能代替自然语言质量审查；主运行期间不修改答复逻辑，作为下一轮修复项保留。

本轮成果门未通过。60条EC自动可执行不等于人工盲测；沟通未人工裁决；实际工具40；本轮没有跨会话记忆/AWM或动态用户实验，A-memory仅去掉上下文包；主案例没有动态故障，复杂子集小。不能称本轮达到≥3项收益、90%整体/85%复杂门或超过Agentforce/Dify。EC随后仅用于修复开发；新确认集与真实故障/长会话/规模路由专项独立登记和计分。

费用只列真实返回Token与请求数，缺失usage标记；没有经确认的账单/价格，不生成货币费用。供应商不可变模型revision未知，复现意味着同配置新推理，不保证逐字一致。

## 复现与保留

```powershell
uv run --project backend --frozen python scripts/benchmark.py setup
uv run --project backend --frozen python scripts/benchmark.py run --experiment step9-deepseek-main --trials 5 --concurrency 4 --soak-seconds 7200
uv run --project backend --frozen python scripts/benchmark.py compare --results artifacts/benchmarks/step9-deepseek-main/case-results.jsonl --baseline B1 --candidate P
uv run --project backend --frozen python scripts/benchmark.py reproduce --manifest artifacts/benchmarks/step9-deepseek-main/source-manifest.json --check-only
uv run --project backend --frozen python scripts/verify_benchmark_recovery.py
```

同实验名仅可恢复相同源码/数据/日程；新推理重跑用新实验名。原始轨迹为本地Git忽略证据，分享前需审查。停止步骤9服务用Compose的stop，保留数据库卷；不要使用down -v或全局prune。原始轨迹、控制器作废原因与容量失败档均保留供审查。
