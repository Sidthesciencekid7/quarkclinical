/**
 * Watching a protocol that is being generated (moved out of the retired
 * GenerateButton wizard step).
 *
 * Real generation runs for minutes, so the backend returns a "generating"
 * protocol immediately and finishes it in the background (a single multi-minute
 * HTTP request gets dropped by the edge proxy: the old "Failed to fetch"). The
 * client polls until it flips to complete / failed. Transient network blips are
 * tolerated (we just keep polling), which is the whole point of not holding one
 * connection open.
 *
 * ~30 min at 3 s. Raised from 200 (10 min) after a real generation was cut off
 * at the old ceiling while the server was still working normally: the
 * deterministic engines each inject locked facts the model must render as
 * tables, so a full protocol takes substantially longer than when the ceiling
 * was chosen. The server has no matching timeout -- it finishes and persists
 * regardless -- so a client ceiling that is too low only ever produces a false
 * failure for work that succeeded.
 */
import { useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import type { GeneratedProtocol } from '../types'

export const GENERATION_POLL_INTERVAL_MS = 3000
export const GENERATION_POLL_MAX_ATTEMPTS = 1200 // 3 s x 1200 = 60 min; a real generation took 24 min (2026-10-25)

/** The server reported `generation_status === 'failed'`. */
export class GenerationFailedError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'GenerationFailedError'
  }
}

/** NOT a failure: the client stopped waiting, the server did not stop working.
 *  Kept distinct so the UI says "still running" instead of a red "failed" for a
 *  protocol that is still being written and will persist normally. */
export class StillRunningError extends Error {
  constructor() {
    super('The client stopped waiting; generation is still running on the server.')
    this.name = 'StillRunningError'
  }
}

export interface PollOptions {
  intervalMs?: number
  maxAttempts?: number
  /** Injectable for tests. Default: `api.getProtocol`. */
  fetchProtocol?: (id: string) => Promise<GeneratedProtocol>
  /** Injectable for tests. Default: an abortable `setTimeout`. */
  sleep?: (ms: number, signal?: AbortSignal) => Promise<void>
  signal?: AbortSignal
  /** Called after every successful poll that found the protocol still generating. */
  onTick?: (attempt: number, protocol: GeneratedProtocol) => void
}

function abortError(): Error {
  const e = new Error('Aborted')
  e.name = 'AbortError'
  return e
}

export function abortableSleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) return reject(abortError())
    const t = setTimeout(() => {
      signal?.removeEventListener('abort', onAbort)
      resolve()
    }, ms)
    const onAbort = () => {
      clearTimeout(t)
      reject(abortError())
    }
    signal?.addEventListener('abort', onAbort, { once: true })
  })
}

export function isAbortError(e: unknown): boolean {
  return typeof e === 'object' && e !== null && (e as { name?: unknown }).name === 'AbortError'
}

/**
 * Poll until the protocol is complete. Resolves with the finished protocol;
 * rejects with `GenerationFailedError` (server said failed), `StillRunningError`
 * (attempt ceiling reached) or an AbortError (signal aborted). The first check
 * is immediate, so reopening a finished conversation resolves without waiting.
 */
export async function pollUntilDone(id: string, options: PollOptions = {}): Promise<GeneratedProtocol> {
  const {
    intervalMs = GENERATION_POLL_INTERVAL_MS,
    maxAttempts = GENERATION_POLL_MAX_ATTEMPTS,
    fetchProtocol = api.getProtocol,
    sleep = abortableSleep,
    signal,
    onTick,
  } = options

  for (let attempt = 0; attempt < maxAttempts; attempt++) {
    if (signal?.aborted) throw abortError()
    if (attempt > 0) await sleep(intervalMs, signal)
    if (signal?.aborted) throw abortError()
    let p: GeneratedProtocol
    try {
      p = await fetchProtocol(id)
    } catch {
      continue // transient blip: the background job is still running server-side
    }
    if (p.generation_status === 'failed') {
      throw new GenerationFailedError(p.generation_error || 'Generation failed. Please try again.')
    }
    if (p.generation_status === 'complete' || p.generation_status == null) return p
    onTick?.(attempt, p)
  }
  throw new StillRunningError()
}

export type GenerationPhase = 'idle' | 'running' | 'complete' | 'failed' | 'still_running'

export interface GenerationPollState {
  phase: GenerationPhase
  protocol: GeneratedProtocol | null
  /** Server-supplied failure text when `phase === 'failed'`. */
  error: string | null
  /** Polls made so far. */
  attempts: number
  /** Milliseconds since polling began (ticks once a second while running). */
  elapsedMs: number
}

const IDLE: GenerationPollState = { phase: 'idle', protocol: null, error: null, attempts: 0, elapsedMs: 0 }

/**
 * Polls a protocol's generation status for as long as the caller is mounted and
 * `enabled`. 3 s interval, 1200 attempts (60 min), then `still_running` (never "failed").
 */
export function useGenerationPoll(
  protocolId: string | null | undefined,
  options: Omit<PollOptions, 'signal' | 'onTick'> & { enabled?: boolean } = {},
): GenerationPollState {
  const { enabled = true, intervalMs, maxAttempts, fetchProtocol, sleep } = options
  const [state, setState] = useState<GenerationPollState>(IDLE)
  const optsRef = useRef({ intervalMs, maxAttempts, fetchProtocol, sleep })
  optsRef.current = { intervalMs, maxAttempts, fetchProtocol, sleep }

  useEffect(() => {
    if (!protocolId || !enabled) {
      setState(IDLE)
      return
    }
    const controller = new AbortController()
    const started = Date.now()
    setState({ ...IDLE, phase: 'running' })

    const clock = setInterval(() => {
      setState(s => (s.phase === 'running' ? { ...s, elapsedMs: Date.now() - started } : s))
    }, 1000)

    pollUntilDone(protocolId, {
      ...optsRef.current,
      signal: controller.signal,
      onTick: attempt => setState(s => (s.phase === 'running' ? { ...s, attempts: attempt + 1 } : s)),
    })
      .then(protocol => {
        if (controller.signal.aborted) return
        setState(s => ({ ...s, phase: 'complete', protocol, error: null, elapsedMs: Date.now() - started }))
      })
      .catch(e => {
        if (controller.signal.aborted || isAbortError(e)) return
        if (e instanceof StillRunningError) {
          setState(s => ({ ...s, phase: 'still_running', elapsedMs: Date.now() - started }))
        } else {
          setState(s => ({
            ...s,
            phase: 'failed',
            error: e instanceof Error ? e.message : 'Generation failed. Please try again.',
            elapsedMs: Date.now() - started,
          }))
        }
      })
      .finally(() => clearInterval(clock))

    return () => {
      controller.abort()
      clearInterval(clock)
    }
  }, [protocolId, enabled])

  return state
}
