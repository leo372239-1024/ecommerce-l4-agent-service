import { useState } from 'react'
import { Alert, Button, Card, Descriptions, Drawer, Form, Input, List, Popconfirm, Space } from 'antd'
import { ReloadOutlined } from '@ant-design/icons'
import { api, label, money, time } from './api'
import type { Case, Facts, Proposal, Session } from './api'
import { useAction, useResource } from './hooks'
import { Blank, FactsView, Feedback, Status } from './components'

export function Approvals({ session }: { session: Session }) {
  const resource = useResource<{ items: Proposal[] }>('/approvals', session, { items: [] })
  const action = useAction(resource.reload)
  const [receipt, setReceipt] = useState<Facts>()
  const [showReceipt, setShowReceipt] = useState(false)
  const decision = (item: Proposal, choice: string) => action.perform(() => api(`/approvals/${item.operation_id}/decision`, session, { expected_revision: item.revision, decision: choice }), choice === 'reject' ? '已拒绝，请查看任务最终结果' : '决定已提交，请核对最终操作回执')
  return <><div className="page-heading"><div><h1>{session.role === 'supervisor' ? '主管审批' : '客户确认'}</h1><p>逐项审查订单、金额、版本和有效期；批准后由受控网关执行。</p></div><Button icon={<ReloadOutlined />} onClick={resource.reload}>刷新</Button></div>
    <Alert type="warning" showIcon title="商家为模拟系统" description="客户确认与主管审批分离。批准、发送和成功是不同状态，以业务回执为准。" /><Feedback error={resource.error || action.error} success={action.success} />
    <List loading={resource.loading} pagination={{ pageSize: 8, showSizeChanger: false }} className="card-list" dataSource={resource.data.items} locale={{ emptyText: <Blank text="暂无待确认或审批提案" /> }} renderItem={item => {
      const expired = new Date(item.expires_at).getTime() <= Date.now()
      const customer = session.role === 'customer' && item.status === 'PENDING'
      const supervisor = session.role === 'supervisor' && item.status === 'CONFIRMED' && item.requires_supervisor
      return <List.Item><Card title={<Space><span>{label[item.action] ?? item.action} · {item.order_id}</span><Status value={item.status} /></Space>}>
        <div className="approval-amount">{money(item.amount_minor)}</div><Descriptions column={{ xs: 1, sm: 2 }} items={[
          { key: 'version', label: '提案版本', children: item.revision }, { key: 'risk', label: '主管审批', children: item.requires_supervisor ? '必需' : '无需' },
          { key: 'entity', label: '订单 / 政策版本', children: `${item.entity_version} / ${item.policy_version}` }, { key: 'expiry', label: '有效期至', children: <span>{time(item.expires_at)}{expired && '（已过期）'}</span> },
          { key: 'id', label: '操作编号', children: item.operation_id },
        ]} /><Space wrap>{(customer || supervisor) && <>
          <Popconfirm title={customer ? '确认该业务操作？' : '批准该业务操作？'} description={`${item.order_id} · ${label[item.action] ?? item.action} · ${money(item.amount_minor)}`} onConfirm={() => decision(item, customer ? 'confirm' : 'approve')}><Button type="primary" disabled={action.busy || expired}>{customer ? '客户确认' : '主管批准'}</Button></Popconfirm>
          <Popconfirm title="拒绝该提案？" onConfirm={() => decision(item, 'reject')}><Button danger disabled={action.busy || expired}>拒绝</Button></Popconfirm>
        </>}<Button disabled={action.busy} onClick={() => { setReceipt(undefined); setShowReceipt(true); void action.perform(async () => setReceipt(await api<Facts>(`/operations/${item.operation_id}`, session)), '') }}>查看操作回执</Button></Space>
        {item.status === 'CONFIRMED' && session.role === 'customer' && <p className="muted">客户已确认，等待主管审批。</p>}
      </Card></List.Item>
    }} /><Drawer title="操作回执" open={showReceipt} onClose={() => setShowReceipt(false)} size="large"><Feedback error={action.error} />{receipt ? <FactsView value={receipt} /> : <p className="muted">{action.busy ? '正在查询…' : '该操作尚无可查询回执。'}</p>}</Drawer>
  </>
}
function CaseDetail({ id, session, reloadList }: { id: string; session: Session; reloadList: () => void }) {
  const resource = useResource<Case | undefined>(`/cases/${id}`, session, undefined)
  const action = useAction(() => { resource.reload(); reloadList() })
  const item = resource.data
  const staff = session.role !== 'customer'
  const mine = item?.status === 'CLAIMED' && item.assigned_to === session.customer_id
  const mutate = (verb: string, extra?: { resolution: string }) => action.perform(() => api(`/cases/${id}/${verb}`, session, { expected_revision: item!.revision, ...extra }), verb === 'close' ? '工单已关闭' : verb === 'claim' ? '工单已由当前账号接管' : '权威对账已完成，请核对操作状态')
  return <><Feedback error={resource.error || action.error} success={action.success} />{item && <><Space><Status value={item.status} /><span>版本 {item.revision}</span></Space><Descriptions className="detail-actions" column={1} items={[
    { key: 'id', label: '工单编号', children: item.id }, { key: 'workflow', label: '来源任务', children: item.workflow_id }, { key: 'orders', label: '订单号', children: item.order_ids?.join('、') || '待补充' },
    { key: 'assigned', label: '接管人', children: item.assigned_to ?? '未分配' }, { key: 'resolution', label: '处理结论', children: item.resolution || '待处理' },
  ]} /><h3>交接资料</h3><FactsView value={item.packet} /><h3>当前业务写入与回执</h3>{item.operation_receipts?.length ? <FactsView value={item.operation_receipts} /> : <p className="muted">没有业务写入记录。</p>}
    {staff && item.status === 'OPEN' && <Button className="detail-actions" type="primary" loading={action.busy} onClick={() => void mutate('claim')}>接管工单</Button>}
    {staff && mine && <><Alert type="info" title="对账依据为模拟商家共享事务" description="核对结果未知的写入后再关闭工单。真实商家接入须提供其权威查询。" />
      <Button className="detail-actions" loading={action.busy} onClick={() => void mutate('reconcile')}>执行权威对账</Button>
      <Form layout="vertical" onFinish={values => void mutate('close', { resolution: String(values.resolution).trim() })}><Form.Item label="处理结论" name="resolution" rules={[{ required: true }, { min: 3, max: 1000 }, { whitespace: true }]}><Input.TextArea aria-label="工单处理结论" maxLength={1000} rows={3} /></Form.Item><Button htmlType="submit" type="primary" disabled={action.busy}>记录结论并关闭</Button></Form>
    </>}
  </>}</>
}
export function Cases({ session }: { session: Session }) {
  const resource = useResource<{ items: Case[] }>('/cases', session, { items: [] })
  const [selected, setSelected] = useState('')
  return <><div className="page-heading"><div><h1>人工工单</h1><p>{session.role === 'customer' ? '查看人工处理进度与结论。' : '接管、核对业务写入，并记录处理结论。'}</p></div><Button icon={<ReloadOutlined />} onClick={resource.reload}>刷新</Button></div><Feedback error={resource.error} />
    <Card><List loading={resource.loading} pagination={{ pageSize: 8, showSizeChanger: false }} dataSource={resource.data.items} locale={{ emptyText: <Blank text="暂无人工工单" /> }} renderItem={item => <List.Item actions={[<Button key="view" onClick={() => setSelected(item.id)}>查看工单</Button>]}><List.Item.Meta title={<Space><Status value={item.status} /><span>{item.id.slice(0, 8)}</span></Space>} description={`创建于 ${time(item.created_at)} · 版本 ${item.revision}`} /></List.Item>} /></Card>
    <Drawer title="人工工单详情" open={!!selected} onClose={() => setSelected('')} size="large">{selected && <CaseDetail key={selected} id={selected} session={session} reloadList={resource.reload} />}</Drawer>
  </>
}


