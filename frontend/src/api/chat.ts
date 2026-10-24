/**
 * Typed client for the chat API (docs/redesign-chat-plan.md B3).
 *
 * Plain routes are JSON request/response. `streamTurn` is the one streaming
 * call: it POSTs with fetch and reads the response body as a ReadableStream,
 * because EventSource cannot send the Authorization header.
 *
 * Errors:
 *  - Every non-2xx response throws a `ChatApiError` (still an `HttpError`, so
 *    `e instanceof Error ? e.message : ...` callers keep working). Use `e.kind`
 *    to tell 409 busy / 402 license / 429 rate limit / 409 stale spec apart.
 *  - `streamTurn` only throws BEFORE the stream opens (HTTP error, or the
 *    request itself failing). Once it is open, everything -- including "the
 *    connection dropped" -- is delivered through `onEvent`, and the promise
 *    resolves. A stream that ends without a `turn_end` or `error` event is
 *    reported as a synthesised `error` event with code `stream_interrupted`
 *    (retryable); the server keeps running the turn, so the caller should
 *    resync with `getChat`. Aborting through `signal` resolves quietly.
 */
import type {
  ActionRequest,
  ActionResponse,
  ActionType,
  ChatSessionDetail,
  ChatSessionListItem,
  ChatSessionRead,
  ChatStreamEvent,
  CreateChatRequest,
  PatchChatRequest,
  ProposalRead,
  SessionKind,
  StreamCheckTick,
  TurnEventName,
  TurnEventMap,
  TurnRequest,
} from '../chat/types'
import { API_BASE, authHeaders, HttpError } from './client'

/* ---------------------------------------------------------------- errors */

/** What kind of failure this is, distilled from the HTTP status and `detail.code`. */
export type ChatApiErrorKind =
  | 'busy'          // 409: another turn is already running in this session
  | 'stale_spec'    // 409: the card the user clicked was built from an older spec
  | 'conflict'      // 409: any other conflict (e.g. proposal_stale)
  | 'license'       // 402: organisation license not active
  | 'rate_limit'    // 429: per-minute limiter, or the daily / per-session turn cap
  | 'unauthorized'  // 401
  | 'forbidden'     // 403
  | 'not_found'     // 404
  | 'validation'    // 422
  | 'server'        // 5xx
  | 'other'

export class ChatApiError extends HttpError {
  kind: ChatApiErrorKind
  /** `detail.code` from the server (e.g. 'stale_spec', 'daily_cap'), when it sent one. */
  code: string | null
  /** From the Retry-After header, when present and readable (cross-origin
   *  responses only expose it if the server lists it in Access-Control-Expose-Headers). */
  retryAfterSeconds: number | null

  constructor(status: number, message: string, kind: ChatApiErrorKind, code: string | null, retryAfterSeconds: number | null) {
    super(status, message)
    this.name = 'ChatApiError'
    this.kind = kind
    this.code = code
    this.retryAfterSeconds = retryAfterSeconds
  }
}

function kindFor(status: number, code: string | null, conflictDefault: ChatApiErrorKind): ChatApiErrorKind {
  if (status === 401) return 'unauthorized'
  if (status === 402) return 'license'
  if (status === 403) return 'forbidden'
  if (status === 404) return 'not_found'
  if (status === 409) {
    if (code === 'busy') return 'busy'
    if (code === 'stale_spec') return 'stale_spec'
    if (code) return 'conflict'
    return conflictDefault
  }
  if (status === 422) return 'validation'
  if (status === 429) return 'rate_limit'
  if (status >= 500) return 'server'
  return 'other'
}

/** Turn a non-2xx response into a ChatApiError. `detail` may be a string (the
 *  existing 402/429 wording), `{code, message}` (chat routes) or FastAPI's
 *  validation array. */
async function chatErrorFrom(res: Response, conflictDefault: ChatApiErrorKind = 'conflict'): Promise<ChatApiError> {
  let message = `${res.status} ${res.statusText}`.trim()
  let code: string | null = null
  try {
    const body = await res.json()
    const detail = body?.detail
    if (typeof detail === 'string') {
      message = detail
    } else if (Array.isArray(detail)) {
      const joined = detail
        .map((d: { msg?: unknown }) => (typeof d?.msg === 'string' ? d.msg : ''))
        .filter(Boolean)
        .join('; ')
      if (joined) message = joined
    } else if (detail && typeof detail === 'object') {
      if (typeof detail.message === 'string') message = detail.message
      if (typeof detail.code === 'string') code = detail.code
    }
  } catch { /* body was not JSON -- keep the status line */ }

  const retryHeader = res.headers.get('Retry-After')
  const retry = retryHeader !== null ? Number.parseInt(retryHeader, 10) : NaN
  return new ChatApiError(
    res.status,
    message,
    kindFor(res.status, code, conflictDefault),
    code,
    Number.isFinite(retry) ? retry : null,
  )
}

/* ----------------------------------------------------------- plain routes */

async function chatRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}/api${path}`, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...(await authHeaders()),
      ...(init?.headers as Record<string, string> | undefined),
    },
  })
  if (!res.ok) throw await chatErrorFrom(res)
  if (res.status === 204) return undefined as T
  return res.json() as Promise<T>
}

const enc = encodeURIComponent

/** Create a session and (optionally) store the first user message. No model
 *  call, no license check -- so the landing-page handoff works for pending orgs. */
export function createChat(body: CreateChatRequest): Promise<ChatSessionRead> {
  return chatRequest<ChatSessionRead>('/chat', { method: 'POST', body: JSON.stringify(body) })
}

/** My sessions, most recently active first. */
export function listChats(params: { kind?: SessionKind; limit?: number } = {}, opts: { signal?: AbortSignal } = {}): Promise<ChatSessionListItem[]> {
  const qs = new URLSearchParams()
  if (params.kind) qs.set('kind', params.kind)
  if (params.limit !== undefined) qs.set('limit', String(params.limit))
  const query = qs.toString()
  return chatRequest<ChatSessionListItem[]>(`/chat${query ? `?${query}` : ''}`, { signal: opts.signal })
}

/** Session, display transcript, cards, spec summary and `awaiting_reply`. Also
 *  the recovery call after a dropped stream. */
export function getChat(sessionId: string, opts: { signal?: AbortSignal } = {}): Promise<ChatSessionDetail> {
  return chatRequest<ChatSessionDetail>(`/chat/${enc(sessionId)}`, { signal: opts.signal })
}

/** Deterministic card action (no AI). 409 with kind 'stale_spec' means the spec
 *  moved on: refetch with getChat and re-render. */
export function sendAction<A extends ActionType>(sessionId: string, body: ActionRequest<A>): Promise<ActionResponse> {
  return chatRequest<ActionResponse>(`/chat/${enc(sessionId)}/actions`, { method: 'POST', body: JSON.stringify(body) })
}

/** Rename and/or archive. */
export function patchChat(sessionId: string, body: PatchChatRequest): Promise<ChatSessionRead> {
  return chatRequest<ChatSessionRead>(`/chat/${enc(sessionId)}`, { method: 'PATCH', body: JSON.stringify(body) })
}

/** Poll an edit proposal; once `status === 'ready'` it carries the diff and QC. */
export function getProposal(sessionId: string, proposalId: string, opts: { signal?: AbortSignal } = {}): Promise<ProposalRead> {
  return chatRequest<ProposalRead>(`/chat/${enc(sessionId)}/proposals/${enc(proposalId)}`, { signal: opts.signal })
}

/* ------------------------------------------------------------ SSE parser */

/** One dispatched SSE message, exactly as the wire carried it. */
export interface SseMessage {
  /** The `event:` name, or 'message' when the frame had none. */
  event: string
  /** All `data:` lines joined with "\n". */
  data: string
  /** Last `id:` value seen on the stream so far, if any. */
  id?: string
}

export interface SseParser {
  /** Feed the next chunk. Strings are used as-is; bytes are decoded as UTF-8
   *  with a streaming decoder, so a multi-byte character split across two
   *  chunks is reassembled correctly. Chunks may split anywhere. */
  feed(chunk: string | Uint8Array): void
  /** Signal end of stream: flushes the decoder. Per the SSE spec, a final
   *  event not terminated by a blank line is discarded. */
  end(): void
}

/**
 * Incremental Server-Sent Events parser (https://html.spec.whatwg.org/multipage/server-sent-events.html#parsing-an-event-stream).
 *
 * - `event:` / `data:` / `id:` fields; `retry:` and unknown fields are ignored.
 * - Multi-line data is joined with "\n"; a single leading space after ':' is dropped.
 * - Lines starting with ':' are comments (heartbeats) and ignored.
 * - A blank line dispatches; a frame with no `data:` lines dispatches nothing.
 * - Line endings: LF, CRLF and bare CR are all accepted, including a CRLF
 *   split across two chunks.
 * - A leading UTF-8 BOM is ignored.
 *
 * Pure: no DOM, no network, no timers. The only environment dependency is
 * `TextDecoder`, used only when bytes are fed.
 */
export function createSseParser(onEvent: (message: SseMessage) => void): SseParser {
  let decoder: TextDecoder | null = null
  let buffer = ''          // text of the current, not yet terminated line
  let skipLeadingLF = false // previous chunk ended in CR: swallow a LF that opens this one
  let atStreamStart = true  // BOM is only meaningful at the very start
  let eventName = ''
  let dataLines: string[] = []
  let lastId: string | undefined

  const processLine = (line: string): void => {
    if (line === '') {
      if (dataLines.length > 0) {
        const message: SseMessage = { event: eventName || 'message', data: dataLines.join('\n') }
        if (lastId !== undefined) message.id = lastId
        onEvent(message)
      }
      eventName = ''
      dataLines = []
      return
    }
    if (line.charCodeAt(0) === 0x3a /* ':' */) return // comment / heartbeat

    const colon = line.indexOf(':')
    const field = colon === -1 ? line : line.slice(0, colon)
    let value = colon === -1 ? '' : line.slice(colon + 1)
    if (value.charCodeAt(0) === 0x20) value = value.slice(1)

    if (field === 'event') eventName = value
    else if (field === 'data') dataLines.push(value)
    else if (field === 'id' && !value.includes('\u0000')) lastId = value
    // 'retry' and unknown fields: ignored
  }

  const feedText = (text: string): void => {
    if (text.length === 0) return
    let start = 0
    if (atStreamStart) {
      atStreamStart = false
      if (text.charCodeAt(0) === 0xfeff) start = 1
    }
    if (skipLeadingLF) {
      skipLeadingLF = false
      if (text.charCodeAt(start) === 0x0a) start += 1
    }

    let lineStart = start
    for (let i = start; i < text.length; i++) {
      const c = text.charCodeAt(i)
      if (c !== 0x0a && c !== 0x0d) continue
      processLine(buffer + text.slice(lineStart, i))
      buffer = ''
      if (c === 0x0d) {
        if (i + 1 < text.length) {
          if (text.charCodeAt(i + 1) === 0x0a) i += 1 // CRLF
        } else {
          skipLeadingLF = true // CR is the last char of the chunk: a LF may open the next
        }
      }
      lineStart = i + 1
    }
    buffer += text.slice(lineStart)
  }

  return {
    feed(chunk) {
      if (typeof chunk === 'string') {
        feedText(chunk)
        return
      }
      if (!decoder) decoder = new TextDecoder('utf-8') // BOM stripped by default
      feedText(decoder.decode(chunk, { stream: true }))
    },
    end() {
      if (decoder) feedText(decoder.decode())
      // Unterminated trailing line / event is discarded, per the SSE spec.
      buffer = ''
      eventName = ''
      dataLines = []
      skipLeadingLF = false
    },
  }
}

/* --------------------------------------------------------- stream plumbing */

function isAbortError(e: unknown): boolean {
  return typeof e === 'object' && e !== null && (e as { name?: unknown }).name === 'AbortError'
}

/** Reads `res.body` to the end, feeding the SSE parser. Resolves 'aborted' if
 *  the caller's signal cancelled it, 'ended' on a clean close, and rethrows any
 *  other read failure. */
async function pumpSse(res: Response, onMessage: (m: SseMessage) => void, signal?: AbortSignal): Promise<'ended' | 'aborted'> {
  if (!res.body) throw new Error('The server returned an empty response body for a stream.')
  const reader = res.body.getReader()
  const parser = createSseParser(onMessage)
  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      if (value) parser.feed(value)
    }
    parser.end()
    return 'ended'
  } catch (e) {
    if (signal?.aborted || isAbortError(e)) return 'aborted'
    throw e
  } finally {
    try { reader.releaseLock() } catch { /* already released */ }
  }
}

function rejectNonStream(res: Response): void {
  const type = (res.headers.get('Content-Type') ?? '').toLowerCase()
  if (type.startsWith('application/json') || type.startsWith('text/html')) {
    throw new ChatApiError(res.status, 'The server did not return an event stream.', 'other', null, null)
  }
}

const TURN_EVENT_NAMES: ReadonlySet<string> = new Set<TurnEventName>([
  'turn_start', 'text_delta', 'tool_start', 'card', 'spec', 'notice', 'turn_end', 'error',
])

/* -------------------------------------------------------------- streamTurn */

export interface StreamTurnOptions {
  /** Called once per server event, in order. Exceptions thrown here are
   *  logged and swallowed so one bad handler cannot cut the stream. */
  onEvent: (event: ChatStreamEvent) => void
  /** Abort to stop listening. The server still finishes and saves the turn. */
  signal?: AbortSignal
}

/**
 * Run one AI turn. POSTs `body` and streams the reply as typed events.
 * See the file header for the error model. Unknown event names and
 * unparseable payloads are skipped (forward compatibility).
 */
export async function streamTurn(sessionId: string, body: TurnRequest, { onEvent, signal }: StreamTurnOptions): Promise<void> {
  let res: Response
  try {
    res = await fetch(`${API_BASE}/api/chat/${enc(sessionId)}/turns`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Accept: 'text/event-stream',
        ...(await authHeaders()),
      },
      body: JSON.stringify(body),
      signal,
    })
  } catch (e) {
    if (signal?.aborted || isAbortError(e)) return
    throw e
  }
  // The only 409 a turn can hit with no code is the turn lock.
  if (!res.ok) throw await chatErrorFrom(res, 'busy')
  rejectNonStream(res)

  let sawTerminal = false
  const emit = (event: ChatStreamEvent): void => {
    try {
      onEvent(event)
    } catch (err) {
      console.error('[chat] onEvent handler threw', err)
    }
  }
  const interrupted = (): void => emit({
    event: 'error',
    data: {
      code: 'stream_interrupted',
      message: 'The connection closed before the reply finished. Reloading the conversation will show where it got to.',
      retryable: true,
    },
  })

  let outcome: 'ended' | 'aborted'
  try {
    outcome = await pumpSse(res, (m) => {
      if (!TURN_EVENT_NAMES.has(m.event)) return
      let data: unknown
      try {
        data = JSON.parse(m.data)
      } catch {
        console.warn('[chat] dropped an unparseable stream event', m.event)
        return
      }
      if (m.event === 'turn_end' || m.event === 'error') sawTerminal = true
      emit({ event: m.event as TurnEventName, data: data as TurnEventMap[TurnEventName] } as ChatStreamEvent)
    }, signal)
  } catch {
    // Network drop mid-stream.
    if (!sawTerminal) interrupted()
    return
  }
  if (outcome === 'aborted') return
  if (!sawTerminal) interrupted()
}

/* ------------------------------------------------------------ streamCheck */

export interface StreamCheckTickResult extends StreamCheckTick {
  /** Milliseconds from sending the request to receiving this tick (client clock). */
  client_ms: number
}

export interface StreamCheckResult {
  ticks: StreamCheckTickResult[]
  /** True when every expected tick (`total`) arrived. */
  complete: boolean
  /**
   * Heuristic: true when the ticks reached the browser in a burst far tighter
   * than the server spaced them (client spread < half the server spread), which
   * means something between the server and the browser held the response back.
   * Only meaningful when `complete` and at least two ticks arrived.
   */
  buffered: boolean
}

const now = (): number => (typeof performance !== 'undefined' ? performance.now() : Date.now())

/** GET /api/chat/stream-check: 5 server-sent ticks over about 20 s, no AI.
 *  Run it from the deployed frontend to prove Railway/CDN does not buffer SSE. */
export async function streamCheck({ onTick, signal }: { onTick?: (tick: StreamCheckTickResult) => void; signal?: AbortSignal } = {}): Promise<StreamCheckResult> {
  const t0 = now()
  const res = await fetch(`${API_BASE}/api/chat/stream-check`, {
    headers: { Accept: 'text/event-stream', ...(await authHeaders()) },
    signal,
  })
  if (!res.ok) throw await chatErrorFrom(res)
  rejectNonStream(res)

  const ticks: StreamCheckTickResult[] = []
  await pumpSse(res, (m) => {
    if (m.event !== 'tick') return
    try {
      const tick = JSON.parse(m.data) as StreamCheckTick
      const result: StreamCheckTickResult = { ...tick, client_ms: Math.round(now() - t0) }
      ticks.push(result)
      onTick?.(result)
    } catch {
      /* skip a malformed tick */
    }
  }, signal)

  const total = ticks.length > 0 ? ticks[0].total : 0
  const complete = total > 0 && ticks.length >= total
  let buffered = false
  if (complete && ticks.length >= 2) {
    const first = ticks[0]
    const last = ticks[ticks.length - 1]
    const serverSpread = last.elapsed_ms - first.elapsed_ms
    const clientSpread = last.client_ms - first.client_ms
    buffered = serverSpread > 0 && clientSpread < serverSpread / 2
  }
  return { ticks, complete, buffered }
}
