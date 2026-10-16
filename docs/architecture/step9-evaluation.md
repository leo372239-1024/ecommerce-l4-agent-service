# 步骤9：可复现比较与终态评分

本轮执行范围和解释边界见[执行登记](../evaluation/step9-preregistration.md)。这是探索性工程比较；60条EC自动案例尚待人工审查，不称独立盲测黄金集。原80条GC仍为候选规格。

## 数据与隔离

`scripts/freeze_benchmark.py`生成静态JSON，包含family、fixture、任务、允许客户回复、终态证据和副作用约束。评测器逐episode创建新订单，独立数据库为`agent_benchmark_step9`。Agent只收到任务和授权后的业务结果；MCP服务只挂载应用代码，不挂载案例JSON/评分器，也不持有模型密钥。控制器和业务服务复用Docker内部网络、PG宿主，并非网络隔离的独立盲测设施。

`infra/benchmark-compose.yml`在原Compose上增加独立MCP、API :8009、按需Worker和控制器。原演示业务库、API :8000及已有卷保留；用户切换DeepSeek后普通API/Worker/MCP更新stage9镜像。控制器只允许指定评测库，评测分区关闭历史记忆/AWM学习和召回。

初始化使用独立连接，command_timeout=30秒；同订单最多三次幂等插入，随后核对完整状态。Agent自身Resources、MerchantService、授权/业务超时不随此改变。初始化时间、重试次数独立记录，不混入Agent耗时。未执行Agent的控制器异常使批次停止并留下interruption，不能悄悄删除失败题。

## 对照执行

`backend/app/evaluation/variants.py`为内部Harness工厂。B0为完整40工具schema的ReAct；B1为dense Top8与按需search_tools；P为现有Harness，另有四个消融。正常API不能通过请求参数选择消融。

B0/B1保存标准assistant/tool消息对并经LangGraph检查点恢复。客户补充节点与模型决策分开，避免interrupt恢复重复发送已完成模型请求。写操作通过同一业务网关生成服务端提案、确认、主管批准、幂等操作及回执，模型不能制造授权。

A-plan用线性动作序列替换编译DAG，保留基础实体/参数核验与业务网关；缺少依赖可导致执行失败，计入其成绩。A-route用dense Top6替换混合召回和精排。A-memory仅移除上下文包；本轮没有长期记忆比较。A-reflect只保留一次暂态读重试；主案例无动态故障，不能由零差异推导恢复能力等价。

RunStore的可选run_id过滤只供评测控制器定向claim，普通Worker不传该参数。每次执行仍更新真实租约/fencing和持久事件。

## 独立终态核验

`oracle.py`检查允许终态、授权实体的工具证据、必需字段、订单目标范围、准确写次数/动作/金额、版本变化、退款账本和确认/主管身份。字段类型严格比较，bool不能冒充整数金额。退款回答“已成功”但没有真实成功操作与正确终态，不能通过。

同名字段必须绑定业务来源，例如退货eligible不能由保修eligible替代。允许预先声明的等价读来源：报价可证明退货资格；订单快照可按已实现政策推导取消/地址资格与剩余余额。不是要求唯一工具执行顺序。自动核验仍不包含最终自然语言全部claim、完整性和体验的人工裁决。

脚本客户只确认任务允许的订单/写动作；其他提案拒绝，客户脚本不读取隐藏oracle。超过5000分由真实主管角色批准。本轮脚本回复不产生模型推理费用，也不能代表动态用户模拟。

## 归档与重现

`scripts/benchmark.py`提供setup/run/compare/reproduce。每批保存源文件内容和逐文件SHA、锁文件、镜像ID、资源配置、dataset/policy/catalog hash、随机日程、真实模型请求用量、终态与轨迹。当前Git工作树没有既有提交，使用hash与`frozen-source/`保留证据，不伪造commit。Secret和环境文件不复制。

供应商返回model/usage逐请求归档，HTTP错误和运输异常均记录。不可获得不可变供应商revision，也未取得实际账单，因此不能声明位级确定性或货币成本。检索请求未返回usage时明确标记缺失。

统计以同case/trial配对，再按family等权cluster bootstrap；重复trial不能充当独立场景。pass^5为五次全部成功。多组比较明确为探索性，原始总episode比例与family口径分开。

长运行若提前完成全部比较，继续执行真实P只读任务至7200秒，单独归档，质量批次结束后按30秒业务到达节奏继续，不扩充质量样本，也不标成持续满载。容量脚本测控制API与排队观察SSE，不能把其RPS/连接数称为同时推理Agent吞吐。恢复套件在主比较结束后串行启动独立Worker并停止，仅操作步骤9服务。

## 发现的MCP运行时校验缺口

工具advertised schema设置additionalProperties=false没有同步改变MCP SDK的运行时参数模型。边界探针发现37个只读工具接受多余tenant_id参数；该参数被忽略，未证明越权读写，但违背公布契约。注册工具时同步将arg_model设为strict与extra=forbid并rebuild，实际40工具×5输入类型重测全部拒绝。该矩阵只有五类相关探针，不能视为200个独立模型注入场景。

修复已进入stage9镜像，并在普通服务与步骤9服务部署。历史ZHIPU_*设置和zhipu_api_key Secret文件名继续兼容，当前值为用户指定DeepSeek接口和deepseek-flash；所有chat请求统一thinking disabled，百炼Embedding/Reranker不变。ReAct支持单响应多工具顺序执行与审批后续接，不依赖供应商是否执行parallel_tool_calls参数。
