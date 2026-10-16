import { useEffect, useRef, useState } from 'react'
import { Alert, Button, Card, Collapse, Form, Input, List, Popconfirm, Progress, Space, Tag, Timeline } from 'antd'
import { ReloadOutlined, SendOutlined } from '@ant-design/icons'
import { api, label, terminal, time } from './api'
import type { AgentEvent, Citation, Envelope, Node, Run, Session } from './api'
import { useAction, useResource, useRunEvents } from './hooks'
import { Blank, BusinessResult, EvidenceLinks, FactsView, Feedback, resultPreview, Status } from './components'

function eventText(event: AgentEvent) {
  if (event.type === 'STEP_STARTED' || event.type === 'STEP_FINISHED') return `${event.type === 'STEP_STARTED' ? '开始' : '结束'} · ${stepLabel(event.stepName ?? '')}`
  if (event.type === 'RUN_STARTED') return '开始执行任务'
  if (event.type === 'RUN_FINISHED') return '本次执行结束'
  const names: Record<string, string> = { 'business.plan': '计划已生成', 'business.task_spec': '任务范围已确定', 'business.task_spec.resolved': '订单范围已补充', 'business.verification': '核验步骤结果', 'business.approval.requested': '等待客户确认', 'business.operation.receipt': '收到商家操作回执', 'business.result': '业务结果已生成', 'business.case.created': '已建立人工工单', 'business.reflection': '调整执行计划', 'context.workflow.reused': '复用已审批 AWM 模板', 'context.retrieved': '已检索授权资料', 'context.budget': '检查上下文容量' }
  return names[event.name ?? '']
}
function stepLabel(id: string) { const parts = id.split(':'); return parts.length > 1 ? `${parts[0]} · ${label[parts[1]] ?? parts[1]}${parts.includes('prepare') ? ' · 准备确认' : ''}` : id }
export function projectPlan(events: Envelope[]) {
  let nodes: Node[] = [], version = 0
  const states: Record<string, string> = {}
  for (const { event } of events) {
    if (event.name === 'business.plan' && event.value) {
      nodes = (event.value.nodes ?? []) as unknown as Node[]; version = Number(event.value.version)
      Object.keys(states).forEach(key => delete states[key])
    }
    if (event.type === 'STEP_STARTED' && event.stepName) states[event.stepName] = '执行中'
    if (event.name === 'business.verification' && event.value) states[String(event.value.node_id)] = event.value.passed ? '已核验' : '核验失败'
    if (event.name === 'business.reflection' && event.value) states[String(event.value.node_id)] = event.value.action === 'handoff' ? '需人工' : '准备重试'
  }
  return { nodes, version, states }
}
function RunDetail({ id, session, listReload }: { id: string; session: Session; listReload: () => void }) {
  const resource = useResource<Run | undefined>(`/runs/${id}`, session, undefined, 3000)
  const stream = useRunEvents(resource.data, session, resource.reload)
  const action = useAction(() => { resource.reload(); listReload() })
  const run = resource.data
  const plan = projectPlan(stream.events)
  const result = run?.result
  const completed = plan.nodes.filter(n => plan.states[n.id] === '已核验').length
  return <Card className="run-detail" title={<Space wrap><span>任务详情</span>{run && <Status value={run.status} />}</Space>} extra={<Button aria-label="刷新任务详情" icon={<ReloadOutlined />} onClick={resource.reload} />}>
    <Feedback error={resource.error || action.error} success={action.success} />
    {!run ? <Blank text={resource.loading ? '正在读取任务' : '任务不可访问'} /> : <>
      <div className="task-id">{run.id}</div><p className="muted">创建于 {time(run.created_at)} · 版本 {run.revision}</p>
      <div className="stream-status" role="status"><span className={stream.connection === '实时连接' ? 'live-dot' : 'idle-dot'} />{stream.connection} · 已接收事件 #{stream.events.at(-1)?.seq ?? 0}{stream.reconnects > 0 && ` · 恢复 ${stream.reconnects} 次`}<Button size="small" onClick={stream.reconnect}>重新连接</Button></div>
      {run.status === 'WAITING_USER' && <Alert type="info" showIcon title={String(result?.question ?? '请补充订单号')} description={<Form className="inline-form" onFinish={values => {
        const order_ids = String(values.orders).trim().split(/[\s,，、]+/).filter(Boolean)
        if (order_ids.length > 3 || order_ids.some(id => !/^[A-Za-z0-9][A-Za-z0-9_-]{2,63}$/.test(id))) return
        void action.perform(() => api(`/runs/${id}/resume`, session, { expected_revision: run.revision, input: { order_ids } }), '已补充，任务继续执行')
      }}><Form.Item name="orders" rules={[{ required: true, message: '请填写订单号' }, { validator: (_, value) => {
        const ids = String(value ?? '').trim().split(/[\s,，、]+/).filter(Boolean)
        return ids.length >= 1 && ids.length <= 3 && ids.every(id => /^[A-Za-z0-9][A-Za-z0-9_-]{2,63}$/.test(id)) ? Promise.resolve() : Promise.reject(new Error('输入 1–3 个订单号，使用逗号或空格分隔'))
      } }]}><Input aria-label="补充订单号" placeholder="ORD-1001，ORD-1002" maxLength={200} /></Form.Item><Button htmlType="submit" type="primary" loading={action.busy}>补充并继续</Button></Form>} />}
      {run.status === 'WAITING_APPROVAL' && <Alert type="warning" showIcon title="业务写操作尚需确认 / 审批" description="到「确认与审批」查看订单、金额和有效期后决定。确认不等于执行成功，请核对最终回执。" />}
      <h3>执行计划 {plan.version > 0 && <Tag>v{plan.version}</Tag>}</h3>
      {plan.nodes.length ? <><Progress percent={Math.round(completed / plan.nodes.length * 100)} status={terminal(run.status) && run.status !== 'SUCCEEDED' ? 'exception' : undefined} /><div className="plan-list">{plan.nodes.map(node => <div className="plan-row" key={node.id}>
        <div><strong>{stepLabel(node.id)}</strong><p className="muted">{node.dependencies.length ? `依赖：${node.dependencies.map(stepLabel).join('、')}` : '起始步骤'}</p></div>
        <Space><Tag color={node.risk === 'write' ? 'orange' : 'default'}>{node.risk === 'write' ? '业务写入' : '查询'}</Tag><Tag color={plan.states[node.id] === '已核验' ? 'green' : undefined}>{plan.states[node.id] ?? (terminal(run.status) ? '未完成' : '待执行')}</Tag></Space>
      </div>)}</div></> : <p className="muted">{run.status === 'WAITING_USER' ? '补充订单号后生成计划。' : terminal(run.status) ? '本任务没有订单执行步骤。' : '正在理解任务并生成计划…'}</p>}
      <h3>业务结果</h3><BusinessResult result={result} />
      {!!result?.knowledge_citations && <EvidenceLinks session={session} citations={result.knowledge_citations as unknown as Citation[]} />}
      <Space wrap className="detail-actions">{!terminal(run.status) && <Popconfirm title="取消该任务？" description="已经提交的业务结果仍会保留，请核对回执。" onConfirm={() => action.perform(() => api(`/runs/${id}/cancel`, session, { expected_revision: run.revision }), '已提交取消请求')}><Button danger disabled={action.busy}>取消任务</Button></Popconfirm>}
        {run.status === 'SUCCEEDED' && plan.nodes.length > 0 && !result?.knowledge_citations && !plan.nodes.some(node => node.tool === 'business_query') && <Button disabled={action.busy} onClick={() => void action.perform(() => api('/workflows/candidates', session, { source_run_id: id }), '已提取 AWM 候选，等待主管验证和审批')}>提取 AWM 候选</Button>}
      </Space>
      <Collapse items={[
        { key: 'events', label: `执行事件（${stream.events.length}）`, children: <Timeline items={stream.events.filter(e => eventText(e.event)).map(e => ({ children: <span>#{e.seq} {eventText(e.event)}</span> }))} /> },
        ...(result?.entries ? [{ key: 'facts', label: '业务证据与回执', children: <FactsView value={result.entries} /> }] : []),
        ...(result?.handoff_case_id ? [{ key: 'case', label: '人工工单编号', children: <span className="break-word">{String(result.handoff_case_id)}</span> }] : []),
      ]} />
    </>}
  </Card>
}
export default function Runs({ session }: { session: Session }) {
  const resource = useResource<{ items: Run[] }>('/runs?limit=100', session, { items: [] })
  const [selected, setSelected] = useState(() => /^#run=[0-9a-f-]{36}$/.test(location.hash) ? location.hash.slice(5) : '')
  const [message, setMessage] = useState('')
  const [conversation, setConversation] = useState<string | null>(null)
  const conversationHydrated = useRef(false)
  useEffect(() => {
    if (conversationHydrated.current || resource.loading) return
    conversationHydrated.current = true
    setConversation(resource.data.items.find(run => run.id === selected)?.conversation_id ?? null)
  }, [resource.loading, resource.data.items, selected])
  const pending = useRef<{ message: string; key: string } | null>(null)
  const action = useAction(resource.reload)
  const select = (id: string) => { setSelected(id); history.replaceState(null, '', `#run=${id}`) }
  const submit = () => void action.perform(async () => {
    const text = message.trim()
    let current = conversation
    if (!current) {
      current = (await api<{ id: string }>('/conversations', session, {})).id
      setConversation(current)
    }
    if (!pending.current || pending.current.message !== text) pending.current = { message: text, key: crypto.randomUUID() }
    const run = await api<Run>('/runs', session, { message: text, conversation_id: current }, 'POST', undefined, { 'Idempotency-Key': pending.current.key })
    pending.current = null; setMessage(''); select(run.id)
  }, '任务已提交')
  return <><div className="page-heading"><div><h1>客户任务工作台</h1><p>查询、售后与知识问答，从意图到可核验的业务结果。</p></div></div>
    <Card className="composer" title={conversation ? "继续对话" : "开始对话"} extra={<Space>{conversation && <Popconfirm title="删除这段对话的记忆？" description="历史任务和业务回执仍保留；之后不能引用这段对话。" onConfirm={() => action.perform(async () => { await api(`/conversations/${conversation}`, session, undefined, "DELETE"); setConversation(null); pending.current = null }, "对话记忆已删除")}><Button danger disabled={action.busy}>删除对话记忆</Button></Popconfirm>}<Button disabled={action.busy} onClick={() => { conversationHydrated.current = true; setConversation(null); pending.current = null; setMessage("") }}>新对话</Button></Space>}><Input.TextArea aria-label="任务内容" value={message} onChange={e => setMessage(e.target.value)} disabled={action.busy} maxLength={4000} showCount autoSize={{ minRows: 3, maxRows: 7 }} placeholder="例如：查询 ORD-1001 的订单状态和物流" /><div className="composer-footer"><span className="muted">最多 3 个订单 · 可说“刚才那个订单”继续查询 · 商家操作为模拟</span><Button type="primary" icon={<SendOutlined />} loading={action.busy} disabled={!message.trim()} onClick={submit}>提交任务</Button></div><Feedback {...action} /></Card>
    <div className="workspace-grid"><Card title={`最近任务（${resource.data.items.length}）`} extra={<Button aria-label="刷新任务列表" icon={<ReloadOutlined />} onClick={resource.reload} />}><Feedback error={resource.error} /><List loading={resource.loading} pagination={{ pageSize: 6, showSizeChanger: false, responsive: true, showLessItems: true }} dataSource={resource.data.items} locale={{ emptyText: <Blank text="提交第一个任务开始体验" /> }} renderItem={run => <List.Item className={`run-list-item ${selected === run.id ? 'selected' : ''}`}><button className="run-select" onClick={() => { select(run.id); setConversation(run.conversation_id ?? null); pending.current = null }} aria-label={`查看任务 ${run.id}`}><Space wrap><Status value={run.status} /><span className="muted">{time(run.created_at)}</span></Space><p>{resultPreview(run.result)}</p><small>{run.id.slice(0, 8)}</small></button></List.Item>} /></Card>
      {selected ? <RunDetail key={selected} id={selected} session={session} listReload={resource.reload} /> : <Card><Blank text="选择任务查看计划、进度和结果" /></Card>}
    </div></>
}



