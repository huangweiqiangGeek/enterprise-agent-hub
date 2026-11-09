export type Frame = { id: string; event: string; data: string }

/** UTF-8, CR/LF/CRLF and multi-line data; EOF never dispatches an incomplete event. */
export class SSEParser {
  private decoder = new TextDecoder()
  private line = ''
  private cr = false
  private id = ''
  private event = ''
  private data: string[] = []
  private size = 0
  constructor(private emit: (frame: Frame) => void) {}
  feed(bytes: Uint8Array) {
    for (const ch of this.decoder.decode(bytes, { stream: true })) {
      if (this.cr && ch === '\n') {
        this.cr = false
        continue
      }
      this.cr = ch === '\r'
      if (ch === '\r' || ch === '\n') {
        this.consumeLine()
        this.line = ''
      } else this.line += ch
      if (++this.size > 262144) throw new Error('SSE_FRAME_TOO_LARGE')
    }
  }
  private consumeLine() {
    if (!this.line) {
      if (this.data.length)
        this.emit({
          id: this.id,
          event: this.event || 'message',
          data: this.data.join('\n'),
        })
      this.event = ''
      this.data = []
      this.size = 0
      return
    }
    if (this.line.startsWith(':')) return
    const split = this.line.indexOf(':')
    const name = split < 0 ? this.line : this.line.slice(0, split)
    let value = split < 0 ? '' : this.line.slice(split + 1)
    if (value.startsWith(' ')) value = value.slice(1)
    if (name === 'data') this.data.push(value)
    if (name === 'event') this.event = value
    if (name === 'id' && !value.includes('\0')) this.id = value
  }
}

export function object(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}
const known = new Set([
  'run.started',
  'message.delta',
  'tool.started',
  'tool.completed',
  'tool.failed',
  'run.completed',
  'run.failed',
  'run.cancelled',
  'run.timed_out',
])
export class RunStream {
  constructor(
    readonly runId: string,
    public seq: number,
    public draft: string,
  ) {}
  apply(frame: Frame): 'duplicate' | 'applied' | 'terminal' | 'unknown' {
    const value: unknown = JSON.parse(frame.data)
    if (
      !object(value) ||
      value.runId !== this.runId ||
      !Number.isSafeInteger(value.seq) ||
      typeof value.seq !== 'number' ||
      value.seq < 1 ||
      typeof value.type !== 'string' ||
      !object(value.data) ||
      frame.id !== `${this.runId}:${value.seq}` ||
      frame.event !== value.type
    )
      throw new Error('SSE_INVALID')
    if (value.seq <= this.seq) return 'duplicate'
    if (value.seq !== this.seq + 1) throw new Error('SSE_GAP')
    const data = value.data
    if (data.schemaVersion !== 1 || !known.has(value.type)) {
      this.seq = value.seq
      return 'unknown'
    }
    if (value.type === 'message.delta') {
      if (
        typeof data.delta !== 'string' ||
        data.isPartial !== true ||
        typeof data.messageId !== 'string' ||
        (data.reset !== undefined && typeof data.reset !== 'boolean')
      )
        throw new Error('SSE_INVALID')
      this.draft = data.reset ? data.delta : this.draft + data.delta
    }
    this.seq = value.seq
    return [
      'run.completed',
      'run.failed',
      'run.cancelled',
      'run.timed_out',
    ].includes(value.type)
      ? 'terminal'
      : 'applied'
  }
}

export const terminal = (status: string) =>
  ['succeeded', 'failed', 'cancelled', 'timed_out'].includes(status)
