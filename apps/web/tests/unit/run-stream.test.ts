import { describe, expect, it } from 'vitest'
import { SSEParser, RunStream, type Frame } from '../../app/utils/run-stream'
function frame(seq: number, type = 'message.delta', extra = {}): Frame {
  return {
    id: `r:${seq}`,
    event: type,
    data: JSON.stringify({
      runId: 'r',
      seq,
      type,
      data: {
        schemaVersion: 1,
        messageId: 'm',
        delta: '你好',
        isPartial: true,
        ...extra,
      },
    }),
  }
}
describe('durable SSE', () => {
  it('decodes split UTF8, CRLF, comments and multiline data at every byte', () => {
    const frames: Frame[] = []
    const parser = new SSEParser((f) => frames.push(f))
    const bytes = new TextEncoder().encode(
      ': heartbeat\r\nid: r:1\r\nevent: message.delta\r\ndata: 你好\r\ndata: 第二行\r\n\r\n',
    )
    for (const byte of bytes) parser.feed(new Uint8Array([byte]))
    expect(frames).toEqual([
      { id: 'r:1', event: 'message.delta', data: '你好\n第二行' },
    ])
  })
  it('handles CR-only and ignores incomplete EOF', () => {
    const frames: Frame[] = []
    const parser = new SSEParser((f) => frames.push(f))
    parser.feed(new TextEncoder().encode('data: first\r\rdata: unfinished'))
    expect(frames.map((f) => f.data)).toEqual(['first'])
  })
  it('does not advance gaps or append duplicates; terminal remains observable', () => {
    const state = new RunStream('r', 0, '')
    expect(() => state.apply(frame(2, 'run.completed'))).toThrow('SSE_GAP')
    expect(state.seq).toBe(0)
    state.apply(frame(1))
    state.apply(frame(1))
    expect(state.draft).toBe('你好')
    expect(state.apply(frame(2, 'run.completed'))).toBe('terminal')
  })
  it('resumes at snapshot seq and supports repaired draft replacement', () => {
    const state = new RunStream('r', 4, 'original')
    state.apply(frame(5, 'message.delta', { reset: true }))
    expect(state.draft).toBe('你好')
    expect(state.apply(frame(3))).toBe('duplicate')
  })
  it('unknown type/version never completes; wrong run and malformed delta fail closed', () => {
    const state = new RunStream('r', 0, '')
    expect(state.apply(frame(1, 'run.completed', { schemaVersion: 2 }))).toBe(
      'unknown',
    )
    expect(() => state.apply({ ...frame(2), id: 'other:2' })).toThrow(
      'SSE_INVALID',
    )
    expect(() => state.apply(frame(2, 'message.delta', { delta: [] }))).toThrow(
      'SSE_INVALID',
    )
    expect(state.seq).toBe(1)
  })
  it('bounds malformed frames', () => {
    expect(() =>
      new SSEParser(() => {}).feed(
        new TextEncoder().encode('a'.repeat(262145)),
      ),
    ).toThrow('SSE_FRAME_TOO_LARGE')
  })
})
