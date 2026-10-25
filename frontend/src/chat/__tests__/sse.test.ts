import { afterEach, describe, expect, it, vi } from 'vitest'
import { createSseParser, streamTurn } from '../../api/chat'
import type { SseMessage } from '../../api/chat'
import type { ChatStreamEvent } from '../types'

function parse(chunks: Array<string | Uint8Array>): SseMessage[] {
  const out: SseMessage[] = []
  const p = createSseParser(m => out.push(m))
  for (const c of chunks) p.feed(c)
  p.end()
  return out
}

const FRAME = 'event: text_delta\ndata: {"text":"Hello"}\n\n'

describe('createSseParser', () => {
  it('parses one frame', () => {
    expect(parse([FRAME])).toEqual([{ event: 'text_delta', data: '{"text":"Hello"}' }])
  })

  it('gives the same result however the bytes are split', () => {
    const two = FRAME + 'event: turn_end\ndata: {"message_id":"m1"}\n\n'
    const whole = parse([two])
    for (let cut = 1; cut < two.length; cut += 3) {
      expect(parse([two.slice(0, cut), two.slice(cut)])).toEqual(whole)
    }
    expect(parse(two.split('').map(c => c))).toEqual(whole)
  })

  it('ignores comment / heartbeat lines and frames without data', () => {
    const out = parse([': ping\n\n', 'event: spec\n\n', FRAME, ': another comment\n', ': ping\n\n'])
    expect(out).toHaveLength(1)
    expect(out[0].event).toBe('text_delta')
  })

  it('joins multi-line data with a newline', () => {
    const out = parse(['event: notice\ndata: line one\ndata: line two\ndata:\ndata: line four\n\n'])
    expect(out[0].data).toBe('line one\nline two\n\nline four')
  })

  it('accepts CRLF, bare CR and a CRLF split across two chunks', () => {
    expect(parse(['event: a\r\ndata: 1\r\n\r\n'])[0]).toEqual({ event: 'a', data: '1' })
    expect(parse(['event: a\rdata: 2\r\r'])[0]).toEqual({ event: 'a', data: '2' })
    const split = parse(['event: a\r', '\ndata: 3\r', '\n\r', '\n'])
    expect(split).toEqual([{ event: 'a', data: '3' }])
  })

  it('defaults the event name, drops one leading space and a BOM', () => {
    const out = parse(['﻿data:  two spaces\n\n'])
    expect(out).toEqual([{ event: 'message', data: ' two spaces' }])
  })

  it('discards an unterminated final frame', () => {
    expect(parse(['event: text_delta\ndata: {"text":"cut"}\n'])).toEqual([])
  })

  it('reassembles a multi-byte character split across byte chunks', () => {
    const bytes = new TextEncoder().encode('event: text_delta\ndata: {"text":"café → ok"}\n\n')
    const cut = bytes.indexOf(0xc3) + 1 // between the two bytes of "e acute"
    const out = parse([bytes.slice(0, cut), bytes.slice(cut)])
    expect(JSON.parse(out[0].data).text).toBe('café → ok')
  })
})

describe('streamTurn', () => {
  afterEach(() => vi.unstubAllGlobals())

  function streamOf(chunks: string[]): Response {
    const enc = new TextEncoder()
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        for (const c of chunks) controller.enqueue(enc.encode(c))
        controller.close()
      },
    })
    return new Response(body, { status: 200, headers: { 'Content-Type': 'text/event-stream' } })
  }

  it('delivers typed events in order, across split chunks and comments', async () => {
    const wire =
      'event: turn_start\ndata: {"turn_id":"t1"}\n\n: ping\n\n' +
      'event: text_delta\ndata: {"text":"Hi "}\n\nevent: text_delta\ndata: {"text":"there"}\n\n' +
      'event: turn_end\ndata: {"message_id":"m1","stop_reason":"end_turn","flags":{"unverified_numbers":[]}}\n\n'
    const chunks = [wire.slice(0, 17), wire.slice(17, 90), wire.slice(90)]
    vi.stubGlobal('fetch', vi.fn(async () => streamOf(chunks)))
    const events: ChatStreamEvent[] = []
    await streamTurn('s1', { text: 'hello', client_turn_id: 'c1' }, { onEvent: e => events.push(e) })
    expect(events.map(e => e.event)).toEqual(['turn_start', 'text_delta', 'text_delta', 'turn_end'])
    expect(events.filter(e => e.event === 'text_delta').map(e => (e.data as { text: string }).text).join('')).toBe('Hi there')
  })

  it('sends the idempotency key and the text, and asks for an event stream', async () => {
    const fetchMock = vi.fn(async () => streamOf(['event: turn_end\ndata: {"message_id":"m","stop_reason":"end_turn","flags":{"unverified_numbers":[]}}\n\n']))
    vi.stubGlobal('fetch', fetchMock)
    await streamTurn('s1', { text: 'hello', client_turn_id: 'abc-1' }, { onEvent: () => {} })
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toMatch(/\/api\/chat\/s1\/turns$/)
    expect(JSON.parse(init.body as string)).toEqual({ text: 'hello', client_turn_id: 'abc-1' })
    expect((init.headers as Record<string, string>).Accept).toBe('text/event-stream')
  })

  it('reports a stream that ends without turn_end as a retryable stream_interrupted error', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => streamOf(['event: text_delta\ndata: {"text":"par"}\n\n'])))
    const events: ChatStreamEvent[] = []
    await streamTurn('s1', { resume: true, client_turn_id: 'c2' }, { onEvent: e => events.push(e) })
    const last = events[events.length - 1]
    expect(last.event).toBe('error')
    expect(last.event === 'error' && last.data).toMatchObject({ code: 'stream_interrupted', retryable: true })
  })

  it('throws a typed busy error for 409 before the stream opens', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ detail: 'busy' }), { status: 409, headers: { 'Content-Type': 'application/json' } })))
    await expect(streamTurn('s1', { text: 'x', client_turn_id: 'c3' }, { onEvent: () => {} })).rejects.toMatchObject({ kind: 'busy', status: 409 })
  })

  it('resolves quietly when aborted', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (_url: string, init: RequestInit) => {
        const enc = new TextEncoder()
        const body = new ReadableStream<Uint8Array>({
          start(controller) {
            controller.enqueue(enc.encode('event: turn_start\ndata: {"turn_id":"t"}\n\n'))
            // A real fetch body errors with an AbortError when its signal aborts.
            init.signal?.addEventListener('abort', () => controller.error(Object.assign(new Error('Aborted'), { name: 'AbortError' })))
          },
        })
        return new Response(body, { status: 200, headers: { 'Content-Type': 'text/event-stream' } })
      }),
    )
    const controller = new AbortController()
    const events: ChatStreamEvent[] = []
    const run = streamTurn('s1', { text: 'x', client_turn_id: 'c4' }, { onEvent: e => { events.push(e); controller.abort() }, signal: controller.signal })
    await expect(run).resolves.toBeUndefined()
    expect(events.map(e => e.event)).toEqual(['turn_start'])
  })
})
