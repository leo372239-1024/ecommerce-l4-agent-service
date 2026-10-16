import { Alert, Button, Descriptions, Drawer, Empty, Space, Tag } from 'antd'
import { useState } from 'react'
import { api, label, time } from './api'
import type { Citation, Facts, Json, Session } from './api'
import { useAction } from './hooks'

export function Status({ value }: { value: string }) {
  const color = ['SUCCEEDED', 'APPROVED', 'READY', 'CLOSED'].includes(value) ? 'green' : ['FAILED', 'REJECTED', 'INVALIDATED'].includes(value) ? 'red' : ['WAITING_USER', 'WAITING_APPROVAL', 'CONFIRMED', 'OPEN'].includes(value) ? 'gold' : 'blue'
  return <Tag color={color}>{label[value] ?? value}</Tag>
}
export function Feedback({ error, success }: { error?: string; success?: string }) {
  return <>{error && <Alert className="feedback" type="error" showIcon title={error} role="alert" />}{success && <Alert className="feedback" type="success" showIcon title={success} role="status" />}</>
}
export function Blank({ text = '暂无记录' }: { text?: string }) { return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={text} /> }
function record(value: Json | undefined): Facts | undefined {
  return value && typeof value === 'object' && !Array.isArray(value) ? value : undefined
}
function businessText(value: string) {
  return value.replace(/(订单 [^\s：:\n]+[：:]\s*)(PAID|SHIPPED|DELIVERED|CANCELLED)\b/g, (_match, prefix: string, status: string) => prefix + (label[status] ?? status))
}
export function resultPreview(result?: Facts) {
  const count = Array.isArray(result?.entries) ? result.entries.map(record).filter(v => v?.kind === 'business_query').length : 0
  return count ? `已核验 ${count} 项业务查询，选择任务查看结果与证据。` : businessText(String(result?.message ?? result?.question ?? '任务处理中')).slice(0, 90)
}
export function BusinessResult({ result }: { result?: Facts }) {
  const entries = Array.isArray(result?.entries) ? result.entries.map(record).filter((v): v is Facts => !!v && v.kind === 'business_query') : []
  const raw = businessText(String(result?.message ?? result?.question ?? result?.error ?? '执行中，结果将自动更新。'))
  const text = entries.length ? raw.split('\n').filter(line => !/^订单 .* 查询结果：\{/.test(line)).join('\n') : raw
  return <div className="result-text" aria-live="polite">{text && <div>{text}</div>}{entries.map((entry, index) => {
    const evidence = record(entry.evidence)
    const tool = String(evidence?.evidence_id ?? '').split(':').at(-1) ?? ''
    return <section className="query-result" key={index}><Space wrap><Tag color="cyan">{String(entry.order_id)}</Tag><strong>{label[tool] ?? '业务查询结果'}</strong></Space><FactsView value={evidence?.fields} />{typeof evidence?.observed_at === 'string' && <small className="muted">查询于 {time(evidence.observed_at)}</small>}</section>
  })}</div>
}
export function FactsView({ value }: { value: Json | undefined }) {
  if (value === undefined || value === null) return <span className="muted">—</span>
  if (typeof value === 'boolean') return <span>{value ? '是' : '否'}</span>
  if (typeof value !== 'object') return <span className="plain-text">{label[String(value)] ?? String(value)}</span>
  if (Array.isArray(value)) return <div className="fact-array">{value.map((item, i) => <div key={i}><FactsView value={item} /></div>)}</div>
  return <dl className="facts">{Object.entries(value).map(([key, item]) => <div key={key}><dt>{label[key] ?? key}</dt><dd><FactsView value={item} /></dd></div>)}</dl>
}
export function EvidenceLinks({ citations, session }: { citations: Pick<Citation, 'chunk_id' | 'title' | 'version'>[]; session: Session }) {
  const [open, setOpen] = useState(false)
  const [evidence, setEvidence] = useState<Citation>()
  const action = useAction()
  const show = (id: string) => {
    setEvidence(undefined); setOpen(true)
    void action.perform(async () => { setEvidence(await api<Citation>(`/knowledge/evidence/${id}`, session)) }, '')
  }
  return <><Space wrap>{citations.map((item, i) => <Button key={item.chunk_id} onClick={() => show(item.chunk_id)}>引用 {i + 1} · {item.title} v{item.version}</Button>)}</Space>
    <Drawer title="知识引用 · 当前有效证据" open={open} onClose={() => setOpen(false)} size="large">
      <Feedback error={action.error} />{action.busy && <p>正在验证引用权限和有效期…</p>}
      {evidence && <><h3>{evidence.title}</h3><Status value={evidence.validation} /><Descriptions column={1} items={[
        { key: 'source', label: '来源', children: evidence.source_name }, { key: 'version', label: '版本', children: evidence.version },
        { key: 'effective', label: '生效时间', children: time(evidence.effective_at) }, { key: 'expiry', label: '过期时间', children: time(evidence.expires_at) },
        { key: 'retrieved', label: '验证时间', children: time(evidence.retrieved_at) }, { key: 'ref', label: '引用编号', children: evidence.evidence_ref },
        { key: 'hash', label: '内容指纹', children: <span className="break-word">{evidence.content_hash}</span> },
      ]} /><blockquote className="evidence-text">{evidence.content}</blockquote></>}
    </Drawer></>
}
