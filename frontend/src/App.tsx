import { lazy, Suspense, useEffect, useState } from 'react'
import { Alert, Badge, Button, Card, ConfigProvider, Form, Input, Layout, Menu, Space, Spin, Tag } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import { BookOutlined, CheckCircleOutlined, CustomerServiceOutlined, LogoutOutlined, NodeIndexOutlined, RobotOutlined, SendOutlined, SettingOutlined } from '@ant-design/icons'
import { api, ApiError, label } from './api'
import type { Session } from './api'
import { Feedback } from './components'
import { useAction, useResource } from './hooks'
const Runs = lazy(() => import('./Runs'))
const Approvals = lazy(() => import('./Business').then(m => ({ default: m.Approvals })))
const Cases = lazy(() => import('./Business').then(m => ({ default: m.Cases })))
const Knowledge = lazy(() => import('./Management').then(m => ({ default: m.Knowledge })))
const Memories = lazy(() => import('./Management').then(m => ({ default: m.Memories })))
const Workflows = lazy(() => import('./Management').then(m => ({ default: m.Workflows })))

type AuthConfig = { demo_login_enabled: boolean; oidc_enabled: boolean }
function Login({ onLogin }: { onLogin: (session: Session) => void }) {
  const [config, setConfig] = useState<AuthConfig>()
  const [configError, setConfigError] = useState('')
  const action = useAction()
  const [form] = Form.useForm()
  useEffect(() => {
    const controller = new AbortController()
    void api<AuthConfig>('/auth/config', undefined, undefined, 'GET', AbortSignal.any([controller.signal, AbortSignal.timeout(10000)])).then(data => { if (!controller.signal.aborted) setConfig(data) }).catch(() => { if (!controller.signal.aborted) setConfigError('登录配置读取失败，请刷新重试。') })
    return () => controller.abort()
  }, [])
  return <div className="login-shell"><div className="login-story"><div className="eyebrow">L4 COMMERCE · SERVICE AGENT</div><h1>让每一个服务请求，<br />都有明确的进展。</h1><p>从订单查询到售后处理，计划可见，步骤可核验，关键业务由客户确认、主管审批。</p><div className="story-stats"><span><strong>01</strong>理解任务</span><span><strong>02</strong>受控执行</span><span><strong>03</strong>核验结果</span></div><Tag color="cyan">商家模拟 · 模型与检索 API 真实</Tag></div>
    <Card className="login-card"><RobotOutlined className="login-icon" /><h2>登录服务工作台</h2><p className="muted">客户 · 客服 · 主管</p><Feedback error={configError || action.error} />
      {!config && !configError && <Spin />}{config?.oidc_enabled && <Button block type="primary" href="/api/v1/auth/oidc/login">使用组织账号登录</Button>}
      {config?.demo_login_enabled && <><Alert className="feedback" type="info" title="本地演示环境" description="账号：alice / bob（客户）、support（客服）、supervisor（主管）。密码由本机部署生成。" /><Form form={form} layout="vertical" onFinish={values => void action.perform(async () => {
        await api('/auth/login', undefined, { username: values.username.trim(), password: values.password })
        form.resetFields(['password'])
        onLogin(await api<Session>('/auth/session'))
      }, '')}><Form.Item name="username" label="账号" rules={[{ required: true, whitespace: true }]}><Input autoComplete="username" maxLength={64} /></Form.Item><Form.Item name="password" label="密码" rules={[{ required: true }]}><Input.Password autoComplete="current-password" maxLength={256} /></Form.Item><Button block type="primary" htmlType="submit" loading={action.busy}>登录</Button></Form></>}
      {config && !config.demo_login_enabled && !config.oidc_enabled && <Alert type="error" title="组织身份提供方尚未配置，请联系部署管理员。" />}
    </Card></div>
}
function Workspace({ session, onLogout }: { session: Session; onLogout: () => void }) {
  const customer = session.role === 'customer'
  const [page, setPage] = useState(customer ? 'runs' : 'cases')
  const health = useResource<{ status: string }>('/health/ready', session, { status: 'checking' })
  const action = useAction()
  const items = [
    ...(customer ? [{ key: 'runs', icon: <SendOutlined />, label: '客户任务' }] : []),
    ...(session.role !== 'support' ? [{ key: 'approvals', icon: <CheckCircleOutlined />, label: '确认与审批' }] : []),
    { key: 'cases', icon: <CustomerServiceOutlined />, label: '人工工单' },
    { key: 'knowledge', icon: <BookOutlined />, label: '知识库' },
    ...(customer ? [{ key: 'memories', icon: <SettingOutlined />, label: '我的记忆' }] : []),
    ...(session.role !== 'support' ? [{ key: 'workflows', icon: <NodeIndexOutlined />, label: 'AWM 模板' }] : []),
  ]
  return <Layout className="app-shell"><Layout.Header className="app-header"><Space><RobotOutlined className="brand-icon" /><span className="brand-title">电商智能客服</span><Tag color="cyan">商家模拟</Tag></Space><Space wrap><Badge status={health.loading ? 'processing' : health.data.status === 'ok' && !health.error ? 'success' : 'warning'} text={health.loading ? '检查服务中' : health.data.status === 'ok' && !health.error ? '服务就绪' : '服务未就绪'} /><Tag>{label[session.role]} · {session.customer_id}</Tag><Button icon={<LogoutOutlined />} loading={action.busy} onClick={() => void action.perform(async () => { await api('/auth/logout', session, undefined, 'POST'); onLogout() }, '')}>退出</Button></Space></Layout.Header>
    <div className="workspace-body"><aside className="sidebar"><div className="sidebar-label">服务工作台</div><Menu selectedKeys={[page]} items={items} onClick={({ key }) => setPage(key)} /><div className="sidebar-note">计划可见<br />关键操作需确认<br />业务结果可核验</div></aside><main className="app-content"><Feedback error={action.error} />{health.error && <Alert className="feedback" type="warning" title="服务连接异常，正在重试" />}
      <Suspense fallback={<Spin tip="正在加载工作台…"><div style={{ minHeight: 120 }} /></Spin>}>{page === 'runs' && customer && <Runs session={session} />}{page === 'approvals' && <Approvals session={session} />}{page === 'cases' && <Cases session={session} />}{page === 'knowledge' && <Knowledge session={session} />}{page === 'memories' && customer && <Memories session={session} />}{page === 'workflows' && <Workflows session={session} />}</Suspense>
    </main></div></Layout>
}
export default function App() {
  const [session, setSession] = useState<Session | null>(null)
  const [loading, setLoading] = useState(true)
  const [expired, setExpired] = useState(false)
  const [bootError, setBootError] = useState('')
  useEffect(() => {
    const controller = new AbortController()
    const expire = () => { controller.abort(); setSession(null); setExpired(true); setLoading(false) }
    window.addEventListener('session-expired', expire)
    void api<Session>('/auth/session', undefined, undefined, 'GET', AbortSignal.any([controller.signal, AbortSignal.timeout(10000)])).then(data => { if (!controller.signal.aborted) { setSession(data); setExpired(false) } }).catch(e => { if (!controller.signal.aborted && !(e instanceof ApiError && e.status === 401)) setBootError('会话读取失败，请刷新重试。') }).finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => { controller.abort(); window.removeEventListener('session-expired', expire) }
  }, [])
  const login = (value: Session) => { setSession(value); setExpired(false); setBootError('') }
  const logout = () => { setSession(null); setExpired(false); history.replaceState(null, '', location.pathname) }
  return <ConfigProvider locale={zhCN} theme={{ token: { colorPrimary: '#137a72', borderRadius: 10, fontFamily: 'Inter, "Segoe UI", "Microsoft YaHei", sans-serif' } }}>
    {loading ? <div className="boot"><Spin size="large" /><p>正在恢复会话…</p></div> : session ? <Workspace key={session.csrf_token} session={session} onLogout={logout} /> : <>{expired && <Alert banner type="info" title="请登录以继续；已有任务会保留。" />}{bootError && <Alert banner type="error" title={bootError} />}<Login onLogin={login} /></>}
  </ConfigProvider>
}
