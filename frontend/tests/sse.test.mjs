import assert from 'node:assert/strict'
import { test } from 'node:test'
import { SSEDecoder, appendEvents } from '../src/sse.ts'

const expected = [
  { seq: 1, event: { type: 'STEP_STARTED', stepName: '理解任务' } },
  { seq: 2, event: { type: 'CUSTOM', name: 'business.plan', value: { nodes: [] } } },
]
const wire = ': heartbeat\r\n\r\nid: 1\r\ndata: {"type":"STEP_STARTED","stepName":"理解任务"}\r\n\r\nid: 2\ndata: {"type":"CUSTOM",\ndata: "name":"business.plan","value":{"nodes":[]}}\n\n'
test('UTF-8, CRLF, comments and multiline data across every byte split', () => {
  const raw = new TextEncoder().encode(wire)
  for (let i = 0; i <= raw.length; i++) {
    const decoder = new TextDecoder(), parser = new SSEDecoder()
    assert.deepEqual([...parser.push(decoder.decode(raw.slice(0, i), { stream: true })), ...parser.push(decoder.decode(raw.slice(i), { stream: true }))], expected)
  }
})
test('one byte at a time preserves Chinese event fields', () => {
  const decoder = new TextDecoder(), parser = new SSEDecoder(), output = []
  for (const byte of new TextEncoder().encode(wire)) output.push(...parser.push(decoder.decode(new Uint8Array([byte]), { stream: true })))
  assert.deepEqual(output, expected)
})
test('incomplete frame is not acknowledged and is replayed after reconnection', () => {
  const parser = new SSEDecoder()
  assert.deepEqual(parser.push('id: 1\ndata: {"type":"RUN_STARTED"}'), [])
  const reconnect = new SSEDecoder()
  assert.deepEqual(reconnect.push('id: 1\ndata: {"type":"RUN_STARTED"}\n\n'), [{ seq: 1, event: { type: 'RUN_STARTED' } }])
})
test('deduplicates replay and refuses sequence gaps', () => {
  assert.deepEqual(appendEvents(expected.slice(0, 1), expected), expected)
  assert.equal(appendEvents(expected, expected), expected)
  assert.throws(() => appendEvents([], expected.slice(1)), /不连续/)
})
test('rejects invalid protocol rather than advancing cursor', () => {
  for (const id of ['0', '-1', 'abc', '9007199254740992']) assert.throws(() => new SSEDecoder().push(`id: ${id}\ndata: {"type":"CUSTOM"}\n\n`))
  assert.throws(() => new SSEDecoder().push('id: 1\ndata: {"value":"no type"}\n\n'))
  assert.throws(() => new SSEDecoder().push('id: 1\ndata: invalid json\n\n'))
})
