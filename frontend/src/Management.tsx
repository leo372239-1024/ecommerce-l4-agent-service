import { useState } from 'react'
import { Alert, Button, Card, Checkbox, Descriptions, Form, Input, InputNumber, List, Modal, Popconfirm, Select, Space, Switch, Tag } from 'antd'
import { ReloadOutlined } from '@ant-design/icons'
import { api, label, time } from './api'
import type { Citation, Document, Facts, Memory, Role, Session, Workflow } from './api'
import { useAction, useResource } from './hooks'
import { Blank, EvidenceLinks, FactsView, Feedback, Status } from './components'

const preferences = { response_style: ['concise', 'detailed'], contact_channel: ['in_app', 'email'], language: ['zh-CN', 'en'] }
export function Memories({ session }: { session: Session }) {
  const resource = useResource<{ items: Memory[]; automatic_memory_enabled: boolean }>('/memories', session, { items: [], automatic_memory_enabled: false })
  const action = useAction(resource.reload)
  const [key, setKey] = useState<keyof typeof preferences>('response_style')
  const [value, setValue] = useState('concise')
  const [days, setDays] = useState(90)
  return <><div className="page-heading"><div><h1>我的记忆</h1><p>管理确认偏好与历史摘要，随时删除或关闭自动记忆。</p></div><Button icon={<ReloadOutlined />} onClick={resource.reload}>刷新</Button></div><Feedback error={resource.error || action.error} success={action.success} />
    <Card title="记忆设置"><Space wrap><Switch aria-label="自动记忆" checked={resource.data.automatic_memory_enabled} loading={action.busy} disabled={resource.loading || !!resource.error} onChange={enabled => void action.perform(() => api('/memories/settings', session, { automatic_memory_enabled: enabled }, 'PUT'))} /><span>自动记忆已完成任务的摘要</span></Space><p className="muted">历史记忆仅作为参考；订单、政策和权限以本次查询为准。</p><Popconfirm title="遗忘全部记忆？" description="全部记忆将删除，自动记忆也会关闭。" onConfirm={() => action.perform(() => api('/memories', session, undefined, 'DELETE'), '已遗忘全部记忆并关闭自动记忆')}><Button danger disabled={action.busy}>遗忘全部</Button></Popconfirm></Card>
    <Card className="section-card" title="保存明确偏好"><Space wrap><Select aria-label="偏好类型" value={key} style={{ width: 140 }} onChange={next => { setKey(next); setValue(preferences[next][0]) }} options={Object.keys(preferences).map(k => ({ value: k, label: label[k] }))} /><Select aria-label="偏好内容" value={value} style={{ width: 130 }} onChange={setValue} options={preferences[key].map(v => ({ value: v, label: label[v] }))} /><InputNumber aria-label="保留天数" min={1} max={365} value={days} onChange={v => setDays(v ?? 90)} suffix="天" /><Button type="primary" disabled={action.busy} onClick={() => void action.perform(() => api('/memories/preferences', session, { key, value, retain_days: days }, 'PUT'))}>保存偏好</Button></Space></Card>
    <Card className="section-card" title="有效记忆"><List loading={resource.loading} pagination={{ pageSize: 8, showSizeChanger: false }} dataSource={resource.data.items} locale={{ emptyText: <Blank text="暂无有效记忆" /> }} renderItem={item => <List.Item actions={[<Popconfirm key="delete" title="删除这条记忆？" onConfirm={() => action.perform(() => api(`/memories/${item.id}`, session, undefined, 'DELETE'), '记忆已删除')}><Button danger disabled={action.busy}>删除记忆</Button></Popconfirm>]}><List.Item.Meta title={<Space><Tag>{item.kind === 'preference' ? '明确偏好' : '历史摘要'}</Tag><span className="muted">到期 {time(item.expires_at)}</span></Space>} description={<><FactsView value={item.content} />{item.source_run_id && <p className="break-word muted">来源任务：{item.source_run_id}</p>}</>} /></List.Item>} /></Card>
  </>
}
type DocumentForm = { title: string; text: string; scope: 'private' | 'tenant'; kind: 'reference' | 'policy'; visible_roles: Role[]; effective_at?: string; expires_at?: string }
export function Knowledge({ session }: { session: Session }) {
  const resource = useResource<{ items: Document[] }>('/knowledge/documents', session, { items: [] })
  const action = useAction(resource.reload)
  const [editing, setEditing] = useState<Document | 'new' | null>(null)
  const [form] = Form.useForm<DocumentForm>()
  const [query, setQuery] = useState('')
  const [search, setSearch] = useState<{ items: Citation[]; diagnostics: Facts }>()
  const [file, setFile] = useState<File>()
  const [uploadTitle, setUploadTitle] = useState('')
  const [uploadScope, setUploadScope] = useState<'private' | 'tenant'>('private')
  const staff = session.role !== 'customer'
  const supervisor = session.role === 'supervisor'
  const edit = (item: Document | 'new') => {
    form.resetFields()
    if (item !== 'new') form.setFieldsValue({ title: item.title, scope: item.scope, kind: item.kind, visible_roles: item.visible_roles, effective_at: item.effective_at, expires_at: item.expires_at ?? '' })
    setEditing(item)
  }
  const submit = async (values: DocumentForm) => {
    const body = { ...values, title: values.title.trim(), text: values.text.trim(), effective_at: values.effective_at ? new Date(values.effective_at).toISOString() : null, expires_at: values.expires_at ? new Date(values.expires_at).toISOString() : null }
    const ok = await action.perform(() => editing === 'new' ? api('/knowledge/documents', session, body) : api(`/knowledge/documents/${(editing as Document).id}`, session, { expected_version: (editing as Document).version, document: body }, 'PUT'), '资料已提交后台入库')
    if (ok) setEditing(null)
  }
  const mutate = (item: Document, verb: string) => action.perform(() => api(`/knowledge/documents/${item.id}${verb === 'delete' ? '' : '/' + verb}`, session, { expected_version: item.version }, verb === 'delete' ? 'DELETE' : 'POST'), verb === 'delete' ? '资料已删除，引用将失效' : verb === 'approve' ? '资料已审核' : '入库重试已提交')
  const searchNow = () => { setSearch(undefined); void action.perform(async () => setSearch(await api('/knowledge/search', session, { query: query.trim(), limit: 5 })), '') }
  return <><div className="page-heading"><div><h1>知识库</h1><p>上传资料、管理版本与检索引用；政策需主管审核后可用于问答。</p></div><Space><Button icon={<ReloadOutlined />} onClick={resource.reload}>刷新</Button><Button type="primary" onClick={() => edit('new')}>新建资料</Button></Space></div><Feedback error={resource.error || action.error} success={action.success} />
    <Card title="检索知识"><Space.Compact block><Input aria-label="知识检索问题" value={query} onChange={e => setQuery(e.target.value)} placeholder="输入问题，检索当前可访问的有效资料" maxLength={1000} onPressEnter={() => { if (query.trim().length >= 2 && !action.busy) searchNow() }} /><Button type="primary" disabled={query.trim().length < 2 || action.busy} onClick={searchNow}>检索</Button></Space.Compact>
      {search && <div className="detail-actions">{search.items.length ? <EvidenceLinks session={session} citations={search.items} /> : <Blank text="未找到授权且有效的知识证据" />}{search.diagnostics.reranker_degraded === true && <Alert type="warning" title="精排暂不可用，已使用召回排序" />}</div>}
    </Card>
    <Card className="section-card" title="上传 TXT / Markdown"><p className="muted">UTF-8 编码；解析文本不超过 100 万字符、1500 段，文件读取不超过 4 MB；不支持 PDF / DOCX。</p><Space wrap>
      <input type="file" accept=".txt,.md" aria-label="知识文件" disabled={action.busy} onChange={e => setFile(e.target.files?.[0])} />
      <Input aria-label="上传资料标题" placeholder="资料标题" maxLength={200} value={uploadTitle} onChange={e => setUploadTitle(e.target.value)} style={{ width: 200 }} />
      <Select aria-label="上传资料范围" value={uploadScope} onChange={setUploadScope} options={[{ value: 'private', label: '个人' }, ...(staff ? [{ value: 'tenant', label: '商家共享' }] : [])]} />
      <Button disabled={!file || !uploadTitle.trim() || action.busy} onClick={() => void action.perform(async () => {
        if (!file || file.size > 4_000_003 || !/\.(txt|md)$/i.test(file.name)) throw new Error('请选择 4 MB 以内的 TXT / Markdown 文件')
        const data = new FormData(); data.set('file', file); data.set('title', uploadTitle.trim()); data.set('scope', uploadScope)
        await api('/knowledge/upload', session, data)
      }, '文件已提交后台入库')}>上传并入库</Button>
    </Space></Card>
    <List loading={resource.loading} pagination={{ pageSize: 8, showSizeChanger: false }} className="card-list" dataSource={resource.data.items} locale={{ emptyText: <Blank text="暂无资料" /> }} renderItem={item => <List.Item><Card title={<Space wrap><span>{item.title}</span><Status value={item.status} /><Status value={item.validation} /></Space>}>
      <p className="muted">v{item.version} · {label[item.scope]} · {label[item.kind]} · 可见角色：{item.visible_roles.map(r => label[r]).join('、')} · 生效 {time(item.effective_at)} · 到期 {time(item.expires_at)}</p>{item.error_code && <Alert type="error" title={`入库失败：${item.error_code}`} />}
      <Space wrap>{item.can_manage && <><Button disabled={action.busy || (item.kind === 'policy' && !supervisor)} onClick={() => edit(item)}>替换版本</Button>{item.status === 'FAILED' && <Button disabled={action.busy} onClick={() => void mutate(item, 'retry')}>重试入库</Button>}{supervisor && item.status === 'READY' && item.validation !== 'APPROVED' && <Button type="primary" disabled={action.busy} onClick={() => void mutate(item, 'approve')}>主管审核资料</Button>}
        <Popconfirm title="删除资料？" description="删除后已有知识引用将失效。" onConfirm={() => mutate(item, 'delete')}><Button danger disabled={action.busy}>删除资料</Button></Popconfirm>
      </>}</Space>
    </Card></List.Item>} />
    <Modal title={editing === 'new' ? '新建知识资料' : '替换为新版本'} open={editing !== null} onCancel={() => { if (!action.busy) setEditing(null) }} footer={null} destroyOnHidden>
      {editing && <Form form={form} layout="vertical" initialValues={{ scope: 'private', kind: 'reference', visible_roles: ['customer', 'support', 'supervisor'] }} onFinish={values => void submit(values)}>
        <Feedback error={action.error} />{editing !== 'new' && <Alert type="warning" title="填写完整的新内容；原引用会失效，新政策需重新审核。" />}
        <Form.Item label="标题" name="title" rules={[{ required: true, whitespace: true, max: 200 }]}><Input maxLength={200} /></Form.Item>
        <Form.Item label="内容" name="text" rules={[{ required: true, whitespace: true, max: 1000000 }]}><Input.TextArea aria-label="知识资料内容" rows={6} maxLength={1000000} /></Form.Item>
        <Form.Item label="范围" name="scope"><Select disabled={editing !== 'new'} options={[{ value: 'private', label: '个人' }, ...(staff ? [{ value: 'tenant', label: '商家共享' }] : [])]} /></Form.Item>
        <Form.Item label="类型" name="kind"><Select disabled={editing !== 'new'} options={[{ value: 'reference', label: '参考资料' }, ...(supervisor ? [{ value: 'policy', label: '政策（需审核）' }] : [])]} /></Form.Item>
        <Form.Item label="可见角色" name="visible_roles" rules={[{ required: true, type: 'array', min: 1, message: '至少选择一种角色' }]}><Checkbox.Group options={(['customer', 'support', 'supervisor'] as Role[]).map(r => ({ value: r, label: label[r] }))} /></Form.Item>
        {(['effective_at', 'expires_at'] as const).map(name => <Form.Item key={name} label={name === 'effective_at' ? '生效时间（留空立即生效）' : '过期时间（可选）'} name={name} rules={[{ validator: (_, v) => !v || (/([+-]\d\d:\d\d|Z)$/.test(v) && Number.isFinite(new Date(v).getTime())) ? Promise.resolve() : Promise.reject(new Error('请输入含时区的 ISO 时间，例如 2026-10-09T20:00:00+08:00')) }]}><Input placeholder="2026-10-09T20:00:00+08:00" /></Form.Item>)}
        <Button htmlType="submit" type="primary" loading={action.busy}>提交入库</Button>
      </Form>}
    </Modal>
  </>
}
export function Workflows({ session }: { session: Session }) {
  const resource = useResource<{ items: Workflow[] }>('/workflows', session, { items: [] })
  const action = useAction(resource.reload)
  const [skill, setSkill] = useState<{ filename: string; content: string }>()
  const supervisor = session.role === 'supervisor'
  return <><div className="page-heading"><div><h1>AWM 工作流模板</h1><p>从成功任务提取候选，经结构验证与主管审批后复用。</p></div><Button icon={<ReloadOutlined />} onClick={resource.reload}>刷新</Button></div><Alert type="info" showIcon title="模板保留当前权限、政策、客户确认和对账约束" description="结构验证通过不代表业务效果提升。客户可在成功任务详情中提取候选。" /><Feedback error={resource.error || action.error} success={action.success} />
    <List loading={resource.loading} pagination={{ pageSize: 8, showSizeChanger: false }} className="card-list" dataSource={resource.data.items} locale={{ emptyText: <Blank text="暂无可见 AWM 模板" /> }} renderItem={item => <List.Item><Card title={<Space wrap><span>{item.template.goals.map(g => label[g] ?? g).join(' + ')}</span><Status value={item.status} /></Space>}>
      <Descriptions column={1} items={[{ key: 'id', label: '模板编号', children: item.id }, { key: 'source', label: '来源任务', children: item.source_run_id }, { key: 'expiry', label: '有效期至', children: time(item.expires_at) }, { key: 'steps', label: '步骤', children: item.template.dag.nodes.map(n => label[n.tool] ?? n.tool).join(' → ') }]} />
      <p className="muted">{item.template.failure_boundary}</p>{item.regression && <p>结构重放验证：{String(item.regression.passed)} 项通过</p>}
      <Space wrap>{supervisor && ['CANDIDATE', 'VALIDATED'].includes(item.status) && <Button disabled={action.busy} onClick={() => void action.perform(() => api(`/workflows/${item.id}/review`, session, { action: 'validate' }), '结构重放验证完成')}>结构验证</Button>}
        {supervisor && item.status === 'VALIDATED' && <Popconfirm title="批准复用该模板？" onConfirm={() => action.perform(() => api(`/workflows/${item.id}/review`, session, { action: 'approve' }), '模板已批准复用')}><Button type="primary" disabled={action.busy}>批准模板</Button></Popconfirm>}
        {supervisor && item.status !== 'DISABLED' && <Popconfirm title="停用该模板？" onConfirm={() => action.perform(() => api(`/workflows/${item.id}/review`, session, { action: 'disable' }), '模板已停用')}><Button danger disabled={action.busy}>停用模板</Button></Popconfirm>}
        {item.status === 'APPROVED' && <Button disabled={action.busy} onClick={() => void action.perform(async () => setSkill(await api(`/workflows/${item.id}/skill`, session)), '')}>查看 / 导出 Skill</Button>}
      </Space>
    </Card></List.Item>} /><Modal title="已审批 Skill" open={!!skill} onCancel={() => setSkill(undefined)} footer={skill && <Button onClick={() => {
      const url = URL.createObjectURL(new Blob([skill.content], { type: 'text/markdown;charset=utf-8' }))
      const anchor = document.createElement('a'); anchor.href = url; anchor.download = skill.filename; anchor.click(); setTimeout(() => URL.revokeObjectURL(url), 1000)
    }}>下载 SKILL.md</Button>}><pre className="skill-preview">{skill?.content}</pre></Modal>
  </>
}


