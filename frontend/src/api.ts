export type Role = 'customer' | 'support' | 'supervisor'
export type Session = { customer_id: string; role: Role; csrf_token: string; demo_only: boolean }
export type Json = null | boolean | number | string | Json[] | { [key: string]: Json }
export type Facts = { [key: string]: Json }
export type Run = { id: string; status: string; revision: number; generation: number; event_seq: number; created_at: string; result: Facts; conversation_id?: string | null; conversation_seq?: number | null }
export type Proposal = { operation_id: string; workflow_id: string; order_id: string; action: string; status: string; revision: number; amount_minor: number; requires_supervisor: boolean; expires_at: string; entity_version: number; policy_version: number }
export type Case = { id: string; workflow_id: string; status: string; revision: number; assigned_to: string | null; created_at: string; packet?: Facts; resolution?: string; order_ids?: string[]; operation_receipts?: Facts[] }
export type Citation = { chunk_id: string; title: string; content: string; version: number; validation: string; source_name: string; evidence_ref: string; effective_at: string; expires_at: string | null; retrieved_at: string; content_hash: string }
export type Document = { id: string; title: string; version: number; status: string; validation: string; kind: 'reference' | 'policy'; scope: 'private' | 'tenant'; can_manage: boolean; visible_roles: Role[]; effective_at: string; expires_at: string | null; error_code: string | null }
export type Memory = { id: string; kind: string; content: Facts; source_run_id: string | null; expires_at: string; validation: string }
export type Workflow = { id: string; status: string; source_run_id: string; fingerprint: string; template: { goals: string[]; dag: { nodes: Node[] }; failure_boundary: string }; regression: Facts | null; expires_at: string }
export type Node = { id: string; tool: string; order_id: string; dependencies: string[]; risk: string }
export type AgentEvent = { type: string; name?: string; stepName?: string; value?: Facts; result?: Facts }
export type Envelope = { seq: number; event: AgentEvent }

const errors: Record<string, string> = {
  invalid_credentials: '账号或密码不正确。', authentication_required: '请先登录。', session_expired: '会话已过期，请重新登录。',
  csrf_failed: '会话校验失败，请重新登录。', tool_catalog_not_ready: '工具目录尚未就绪，请稍后重试。',
  login_rate_limit: '登录尝试过于频繁，请一分钟后重试。', demo_login_disabled_in_production: '生产环境请使用组织登录。',
  proposal_invalidated_reconfirm_new_intent: '订单、政策或有效期已变化，请重新提交意图并确认新提案。',
  unknown_operations_require_authoritative_reconciliation: '存在结果未知的操作，请先执行权威对账。',
  workflow_scope_not_supported: '该任务不适合作为 AWM 模板，请选择已验证的订单、物流或售后任务。',
  source_trace_version_drift: '来源任务的政策或工具版本已变化，请从新的成功任务提取模板。',
  knowledge_evidence_not_found: '引用已失效或不可访问，请重新检索当前资料。',
  invalid_document_upload: '上传失败，请使用 UTF-8 编码的 TXT 或 Markdown 文件。',
  conversation_not_found: '对话已过期、已删除或不可访问，请开始新对话。',
  conversation_task_in_progress: '这段对话还有任务在执行或等待确认，请先处理该任务，或开始新对话。',
  conversation_turn_limit: '这段对话已达到轮数上限，请开始新对话。',
  conversation_limit: '保留的对话过多，请先删除不需要的对话。',
}
export class ApiError extends Error {
  constructor(public status: number, public code: string) {
    super(errors[code] ?? (status === 409 ? '数据已变化，请刷新后重新审查并操作。' : status === 403 ? '当前账号没有该操作权限。' : status === 413 ? '内容超过大小限制。' : status === 422 ? '输入不符合要求，请检查内容。' : status >= 500 ? '服务暂时不可用，请稍后重试。' : `请求失败（${status}）。`))
  }
}
export async function checkResponse(response: Response) {
  if (response.ok) return
  const body = await response.json().catch(() => ({}))
  const error = new ApiError(response.status, typeof body.detail === 'string' ? body.detail : 'request_failed')
  if ((response.status === 401 && error.code !== 'invalid_credentials') || error.code === 'csrf_failed') window.dispatchEvent(new Event('session-expired'))
  throw error
}
export async function api<T>(path: string, session?: Session | null, body?: unknown, method = body === undefined ? 'GET' : 'POST', signal?: AbortSignal, extra?: Record<string, string>): Promise<T> {
  const multipart = body instanceof FormData
  const response = await fetch(`/api/v1${path}`, {
    method, credentials: 'same-origin', cache: 'no-store', signal: signal ?? AbortSignal.timeout(multipart ? 120000 : 30000),
    headers: { ...(body !== undefined && !multipart ? { 'Content-Type': 'application/json' } : {}), ...(session ? { 'X-CSRF-Token': session.csrf_token } : {}), ...extra },
    body: body === undefined ? undefined : multipart ? body : JSON.stringify(body),
  })
  if (signal?.aborted) throw new DOMException('Aborted', 'AbortError')
  await checkResponse(response)
  return response.status === 204 ? undefined as T : response.json()
}
export const terminal = (status: string) => ['SUCCEEDED', 'FAILED', 'PARTIAL', 'HANDOFF', 'CANCELLED'].includes(status)
export const label: Record<string, string> = {
  get_order_items:'订单商品', get_order_invoice:'发票', get_order_payment:'支付信息', get_order_discounts:'优惠分摊', get_order_address_masked:'收货地址', get_order_timeline:'订单时间线',
  get_shipment_tracking:'物流轨迹', get_shipment_packages:'包裹信息', get_carrier_contact:'承运商', get_delivery_estimate:'预计送达', get_product_specs:'商品规格', get_product_inventory:'商品库存', get_product_warranty:'保修说明', get_product_price:'商品价格', get_product_shipping:'商品配送', get_return_status:'退货进度', get_return_instructions:'退货说明', get_return_shipping_fee:'退货运费', get_refund_balance:'退款余额', get_refund_eta:'退款到账时效', get_refund_status:'退款状态', get_loyalty_impact:'积分影响', get_coupon_return_policy:'优惠券返还', get_coupon_rules:'优惠券规则', get_warranty_eligibility:'保修资格', get_cancellation_eligibility:'取消资格', get_address_change_eligibility:'改址资格', get_exchange_options:'换货选项', get_reship_status:'补发进度', get_support_hours:'客服时间', get_ticket_status:'工单进度',
  nodes:'轨迹节点', tracking_id:'物流单号', timestamp:'时间', estimate:'预计送达日期', delayed:'是否延迟', material:'材质', capacity_ml:'容量（毫升）', sku:'商品编号', available_quantity:'可用库存', days:'保修天数', coverage:'保修范围', eligible:'是否符合', elapsed_days:'已过天数', warranty_days:'保修天数', remaining_minor:'剩余可退金额（分）', refunded_minor:'已退款（分）', points_to_reverse:'需扣回积分', basis:'计算依据', hours:'服务时间', timezone:'时区', type:'类型', warehouse:'退货仓', instructions:'操作说明', shipping_address:'寄回地址', payer:'承担方', fee_minor:'费用（分）', currency:'币种', min_business_days:'最少工作日', max_business_days:'最多工作日', method:'支付方式', restored_on_full_refund:'整单退款返还', expires_unchanged:'有效期不变', regions:'配送区域', province:'省份', city:'城市', address:'地址', phone:'电话', packages:'包裹', weight_grams:'重量（克）', package_id:'包裹编号', sku_ids:'商品编号', tickets:'工单', payment_id:'支付编号', current_price_minor:'当前价格（分）', quantity:'数量', name:'商品名', options:'选项', events:'时间线', transaction_id:'交易编号', return_id:'退货编号', reshipments:'补发包裹', hotline:'联系电话', carrier:'承运商', is_simulated:'是否模拟', NOT_ISSUED:'未开具', NOT_REFUNDED:'尚未退款', REFUNDED:'已退款', ACCEPTED:'已验收', REQUESTED:'已申请', NONE:'未申请', SIMULATED_CARD:'模拟银行卡', PAID:'已支付', SHIPPED:'已发货', DELIVERED:'已签收', CNY:'人民币', remaining_paid_yuan:'剩余实付金额（元）',
  QUEUED: '排队中', RUNNING: '执行中', WAITING_USER: '等待补充', WAITING_APPROVAL: '等待确认 / 审批', SUCCEEDED: '已完成', PARTIAL: '部分完成', FAILED: '失败', HANDOFF: '已转人工', CANCELLED: '已取消',
  PENDING: '待客户确认', CONFIRMED: '待主管审批', APPROVED: '已批准', REJECTED: '已拒绝', INVALIDATED: '已失效', PREPARED: '准备中', DISPATCHED: '已发送', RECONCILING: '对账中',
  OPEN: '待接管', CLAIMED: '处理中', CLOSED: '已关闭', READY: '可检索', INDEXING: '入库中', CANDIDATE: '候选', VALIDATED: '结构验证通过', DISABLED: '已停用', UNREVIEWED: '未经审核', EXECUTED: '已执行',
  get_order: '查询订单', get_logistics: '查询物流', get_policy: '核对政策', check_eligibility: '检查售后资格', quote_refund: '试算退款', business_query: '查询业务信息', submit_refund: '提交退款', create_return: '申请退货', cancel_order: '取消订单',
  order: '订单查询', logistics: '物流查询', eligibility: '售后资格', refund_quote: '退款试算', knowledge: '知识查询', customer: '客户', support: '客服', supervisor: '主管', private: '个人', tenant: '商家共享', reference: '参考资料', policy: '政策',
  concise: '简洁', detailed: '详细', in_app: '站内', email: '邮件', 'zh-CN': '中文', en: '英文', response_style: '回复风格', contact_channel: '联系渠道', language: '语言',
  status: '状态', amount_minor: '金额（分）', paid_minor: '实付（分）', operation_id: '操作编号', order_id: '订单号', kind: '类型', action: '操作', error: '问题', simulated_business: '商家模拟', resolution: '处理结果',
  proposal_status: '提案状态', execution_status: '执行状态', receipt: '业务回执', completed_at: '完成时间', historical_only: '仅历史参考', has_unresolved_steps: '存在未完成步骤', goals: '目标', tools: '查询工具',
}
export const time = (value?: string | null) => value ? new Date(value).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false }) : '—'
export const money = (minor: number) => `¥${(minor / 100).toFixed(2)}`
