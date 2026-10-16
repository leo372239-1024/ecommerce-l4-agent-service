import { useCallback, useEffect, useRef, useState } from 'react'
import { api, ApiError, checkResponse, terminal } from './api'
import type { Envelope, Run, Session } from './api'
import { appendEvents, SSEDecoder } from './sse'

export function useResource<T>(path: string, session: Session, initial: T, interval = 5000) {
  const [data, setData] = useState(initial)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [version, setVersion] = useState(0)
  const reload = useCallback(() => setVersion(v => v + 1), [])
  useEffect(() => {
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout>
    const read = async () => {
      try {
        const next = await api<T>(path, session, undefined, 'GET', AbortSignal.any([controller.signal, AbortSignal.timeout(10000)]))
        if (!controller.signal.aborted) { setData(next); setError('') }
      } catch (e) { if (!controller.signal.aborted) setError(e instanceof ApiError ? e.message : '连接失败，请检查网络或重试。') }
      finally { if (!controller.signal.aborted) { setLoading(false); if (interval) timer = setTimeout(read, interval) } }
    }
    void read()
    return () => { controller.abort(); clearTimeout(timer) }
  }, [path, session, interval, version])
  return { data, error, loading, reload }
}
export function useAction(refresh?: () => void) {
  const lock = useRef(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')
  const perform = async (action: () => Promise<unknown>, message = '已保存') => {
    if (lock.current) return false
    lock.current = true; setBusy(true); setError(''); setSuccess('')
    try { await action(); setSuccess(message); return true }
    catch (e) { setError(e instanceof ApiError ? e.message : '连接中断，结果尚未确认。请刷新后核对，再重试。'); return false }
    finally { lock.current = false; setBusy(false); refresh?.() }
  }
  return { busy, error, success, perform }
}
export function useRunEvents(run: Run | undefined, session: Session, reload: () => void) {
  const [events, setEvents] = useState<Envelope[]>([])
  const [connection, setConnection] = useState('正在连接')
  const [reconnects, setReconnects] = useState(0)
  const [retry, setRetry] = useState(0)
  const buffer = useRef<Envelope[]>([])
  const selected = useRef('')
  useEffect(() => {
    if (!run) return
    if (selected.current !== run.id) {
      selected.current = run.id; buffer.current = []; setEvents([]); setReconnects(0)
    }
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout>
    let failures = 0
    const pump = async () => {
      let reader: ReadableStreamDefaultReader<Uint8Array> | undefined
      try {
        setConnection(buffer.current.length ? '正在恢复事件' : '正在连接')
        const cursor = buffer.current.at(-1)?.seq ?? 0
        const response = await fetch(`/api/v1/runs/${run.id}/events`, {
          credentials: 'same-origin', cache: 'no-store', headers: { 'Last-Event-ID': String(cursor) },
          signal: AbortSignal.any([controller.signal, AbortSignal.timeout(35000)]),
        })
        await checkResponse(response)
        if (!response.headers.get('Content-Type')?.includes('text/event-stream') || !response.body) throw new Error('事件流不可用')
        setConnection('实时连接'); reader = response.body.getReader()
        const decoder = new TextDecoder(), parser = new SSEDecoder()
        while (!controller.signal.aborted) {
          const { value, done } = await reader.read()
          if (done) break
          const batch = parser.push(decoder.decode(value, { stream: true }))
          if (batch.length) {
            buffer.current = appendEvents(buffer.current, batch)
            setEvents(buffer.current); failures = 0
          }
        }
        if (controller.signal.aborted) return
        const latest = await api<Run>(`/runs/${run.id}`, session, undefined, 'GET', AbortSignal.any([controller.signal, AbortSignal.timeout(10000)]))
        if (controller.signal.aborted) return
        reload()
        const caughtUp = (buffer.current.at(-1)?.seq ?? 0) >= latest.event_seq
        if (caughtUp && (terminal(latest.status) || ['WAITING_USER', 'WAITING_APPROVAL'].includes(latest.status))) {
          setConnection('事件已同步'); return // Snapshot polling restarts this pump when new events arrive.
        }
        timer = setTimeout(pump, 600)
      } catch (e) {
        if (controller.signal.aborted) return
        if (e instanceof ApiError && [401, 403, 404].includes(e.status)) { setConnection(e.message); return }
        if (e instanceof ApiError && e.code === 'invalid_event_cursor') { buffer.current = []; setEvents([]) }
        failures++; setReconnects(n => n + 1); setConnection('连接中断，自动重连中')
        timer = setTimeout(pump, Math.min(15000, 1000 * 2 ** Math.min(failures - 1, 4)))
      } finally { if (reader) { await reader.cancel().catch(() => {}); reader.releaseLock() } }
    }
    void pump()
    return () => { controller.abort(); clearTimeout(timer) }
    // event_seq is watched by the snapshot poll so approval in another browser also resumes streaming.
  }, [run?.id, run?.generation, run?.event_seq, session, reload, retry])
  return { events, connection, reconnects, reconnect: () => { setReconnects(n => n + 1); setRetry(n => n + 1) } }
}
