import type { AgentEvent, Envelope } from './api'

// Stateful decoder handles arbitrary UTF-8/line/frame boundaries and ignores heartbeat comments.
export class SSEDecoder {
  private buffer = ''
  private id = ''
  private data: string[] = []
  push(text: string): Envelope[] {
    this.buffer += text
    const output: Envelope[] = []
    let end: number
    while ((end = this.buffer.indexOf('\n')) !== -1) {
      const line = this.buffer.slice(0, end).replace(/\r$/, '')
      this.buffer = this.buffer.slice(end + 1)
      if (!line) {
        if (this.data.length) {
          const seq = Number(this.id)
          const event = JSON.parse(this.data.join('\n')) as AgentEvent
          if (!Number.isSafeInteger(seq) || seq < 1 || typeof event.type !== 'string') throw new Error('事件格式异常')
          output.push({ seq, event })
        }
        this.id = ''; this.data = []
      } else if (line.startsWith('id:')) this.id = line.slice(3).trim()
      else if (line.startsWith('data:')) this.data.push(line.slice(5).replace(/^ /, ''))
    }
    return output
  }
}
export function appendEvents(current: Envelope[], incoming: Envelope[]): Envelope[] {
  let seq = current.at(-1)?.seq ?? 0
  const accepted: Envelope[] = []
  for (const item of incoming) {
    if (item.seq <= seq) continue
    if (item.seq !== seq + 1) throw new Error('事件序号不连续，正在重放')
    accepted.push(item); seq = item.seq
  }
  return accepted.length ? [...current, ...accepted] : current
}
