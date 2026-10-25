/**
 * One chat conversation: loads it, runs turns over SSE, applies card actions.
 *
 * Recovery model (the server is the source of truth and always finishes a turn
 * it started, even when the browser goes away):
 *  - the page is left mid-turn: the stream is aborted; on return `getChat`
 *    shows the saved reply, or `awaiting_reply` runs the turn again once.
 *  - the stream drops (`stream_interrupted`), or the user presses Stop: the
 *    client keeps polling `getChat` (every 2 s, up to ~90 s) until the
 *    transcript stops changing.
 *  - another tab/turn holds the session (409 busy): same polling.
 *  - a card was built from an older spec (409 stale_spec): refetch, toast.
 */
import { useCallback, useEffect, useMemo, useReducer, useRef } from 'react'
import { ChatApiError, getChat, sendAction, streamTurn } from '../api/chat'
import {
  chatReducer,
  describeFailure,
  initialChatState,
  isLocalId,
  selectThread,
} from './chatReducer'
import type { ChatState, ThreadRow } from './chatReducer'
import type { ActionPayloadMap, ActionResponse, ActionType, ChatSessionDetail, TurnRequest } from './types'

export const RECOVER_INTERVAL_MS = 2000
export const RECOVER_MAX_POLLS = 45

export function newClientTurnId(): string {
  const c = typeof crypto !== 'undefined' ? (crypto as Crypto & { randomUUID?: () => string }) : undefined
  if (c?.randomUUID) return c.randomUUID()
  return `t${Date.now().toString(36)}${Math.random().toString(36).slice(2, 10)}`
}

function isAbortError(e: unknown): boolean {
  return typeof e === 'object' && e !== null && (e as { name?: unknown }).name === 'AbortError'
}

function sleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) return reject(Object.assign(new Error('Aborted'), { name: 'AbortError' }))
    const t = setTimeout(() => {
      signal.removeEventListener('abort', onAbort)
      resolve()
    }, ms)
    const onAbort = () => {
      clearTimeout(t)
      reject(Object.assign(new Error('Aborted'), { name: 'AbortError' }))
    }
    signal.addEventListener('abort', onAbort, { once: true })
  })
}

/** A cheap fingerprint of a transcript: changes whenever the server saves something. */
function fingerprint(detail: ChatSessionDetail): string {
  const last = detail.messages[detail.messages.length - 1]
  return `${detail.messages.length}:${last?.id ?? ''}:${detail.cards.length}:${detail.spec.spec_version}`
}

export interface UseChatSessionOptions {
  /** Show a short toast (e.g. "That card was out of date"). */
  notify?: (message: string) => void
  /** Called after a card action succeeded (the ChatDrawer reloads the editor on `apply_edit`). */
  onActionSuccess?: (action: ActionType, response: ActionResponse) => void
  /** Run a turn automatically when the conversation loads with an unanswered message (default true). */
  autoResume?: boolean
}

export interface ChatSessionApi {
  sessionId: string
  state: ChatState
  thread: ThreadRow[]
  /** A reply is streaming, or the client is waiting for the server to finish one. */
  busy: boolean
  send: (text: string) => Promise<void>
  /** Stop listening to the current reply. The server still finishes and saves it. */
  stop: () => void
  /** Run the unanswered message again (after a retryable error). */
  retry: () => Promise<void>
  /** Refetch the whole conversation from the server. */
  resync: () => Promise<void>
  dispatchAction: <A extends ActionType>(cardId: string, action: A, payload: ActionPayloadMap[A]) => Promise<ActionResponse | null>
  dismissBanner: () => void
  dismissTurnError: () => void
  clearRejectedText: () => void
}

export function useChatSession(sessionId: string, options: UseChatSessionOptions = {}): ChatSessionApi {
  const [state, dispatch] = useReducer(chatReducer, initialChatState)
  const stateRef = useRef(state)
  stateRef.current = state
  const optionsRef = useRef(options)
  optionsRef.current = options

  const mountedRef = useRef(true)
  const streamingRef = useRef(false)
  const recoveringRef = useRef(false)
  const actionBusyRef = useRef(false)
  const abortRef = useRef<AbortController | null>(null)
  const recoverAbortRef = useRef<AbortController | null>(null)
  const userStoppedRef = useRef(false)
  const lastSentTextRef = useRef<string | null>(null)
  const resumedForRef = useRef<string | null>(null)
  /** Bumped whenever the user changes the conversation (a message, a card click). */
  const epochRef = useRef(0)

  /* -------------------------------------------------------------- recover */

  const recover = useCallback(async (): Promise<void> => {
    if (recoveringRef.current || !mountedRef.current) return
    recoveringRef.current = true
    dispatch({ type: 'recovering', value: true })
    const ctrl = new AbortController()
    recoverAbortRef.current = ctrl
    let previous: string | null = null
    let stable = 0
    try {
      for (let i = 0; i < RECOVER_MAX_POLLS; i++) {
        if (i > 0) await sleep(RECOVER_INTERVAL_MS, ctrl.signal)
        let detail: ChatSessionDetail
        try {
          detail = await getChat(sessionId, { signal: ctrl.signal })
        } catch (e) {
          if (ctrl.signal.aborted || isAbortError(e)) return
          continue
        }
        const fp = fingerprint(detail)
        stable = fp === previous ? stable + 1 : 0
        previous = fp
        // Done once nothing has changed for two polls and the last message is answered.
        const settled = !detail.awaiting_reply && stable >= 1
        const gaveUp = detail.awaiting_reply && i > 3 && stable >= 3 // never answered: stop waiting, offer Retry
        // While the server is still working, only take what is NEW: replacing the transcript with
        // one that does not yet hold the reply being written would blank the partial text on screen.
        const known = stateRef.current.entries.filter(e => e.kind === 'message' && !isLocalId(e.message.id)).length
        if (settled || gaveUp || detail.messages.length > known) dispatch({ type: 'loaded', detail })
        if (settled || gaveUp) return
      }
    } catch (e) {
      if (!isAbortError(e)) console.error('[chat] recovery stopped', e)
    } finally {
      recoveringRef.current = false
      if (recoverAbortRef.current === ctrl) recoverAbortRef.current = null
      if (mountedRef.current) {
        dispatch({ type: 'recovering', value: false })
        // "Another reply is still running" is no longer true once we have caught up with it.
        if (stateRef.current.banner?.kind === 'busy') dispatch({ type: 'set_banner', banner: null })
      }
    }
  }, [sessionId])

  /* ----------------------------------------------------------------- turns */

  const runTurn = useCallback(
    async (req: { text: string } | { resume: true }): Promise<void> => {
      if (streamingRef.current || recoveringRef.current) return
      streamingRef.current = true
      userStoppedRef.current = false
      epochRef.current++
      const clientTurnId = newClientTurnId()
      const userText = 'text' in req ? req.text : null
      if (userText) lastSentTextRef.current = userText
      dispatch({ type: 'turn_begin', localId: clientTurnId, userText })

      const controller = new AbortController()
      abortRef.current = controller
      const body: TurnRequest = 'text' in req ? { text: req.text, client_turn_id: clientTurnId } : { resume: true, client_turn_id: clientTurnId }

      let interrupted = false
      try {
        await streamTurn(sessionId, body, {
          signal: controller.signal,
          onEvent: event => {
            if (event.event === 'error' && event.data.code === 'stream_interrupted') interrupted = true
            dispatch({ type: 'stream_event', event })
          },
        })
        if (controller.signal.aborted) {
          dispatch({ type: 'turn_finish' })
          // Pressing Stop leaves the server finishing the reply: pick it up quietly.
          if (userStoppedRef.current && mountedRef.current) void recover()
        } else if (interrupted && mountedRef.current) {
          void recover()
        }
      } catch (e) {
        if (isAbortError(e)) {
          dispatch({ type: 'turn_finish' })
        } else {
          const kind = e instanceof ChatApiError ? e.kind : 'other'
          const code = e instanceof ChatApiError ? e.code : null
          const message = e instanceof Error ? e.message : 'The reply could not be started.'
          const failure = describeFailure(kind, code, message)
          // The server rejected the turn before saving anything: put the text back.
          dispatch({
            type: 'turn_rejected',
            localId: clientTurnId,
            userText,
            error: failure.error,
            banner: failure.banner,
          })
          if (kind === 'busy' && mountedRef.current) void recover()
        }
      } finally {
        streamingRef.current = false
        if (abortRef.current === controller) abortRef.current = null
      }
    },
    [sessionId, recover],
  )

  const send = useCallback(
    async (text: string): Promise<void> => {
      const trimmed = text.trim()
      if (!trimmed) return
      await runTurn({ text: trimmed })
    },
    [runTurn],
  )

  const stop = useCallback(() => {
    userStoppedRef.current = true
    abortRef.current?.abort()
  }, [])

  const resync = useCallback(async (): Promise<void> => {
    if (streamingRef.current) return
    // A refetch that started before a newer click or message was sent would overwrite it with older data.
    const epoch = epochRef.current
    try {
      const detail = await getChat(sessionId)
      if (mountedRef.current && !streamingRef.current && epoch === epochRef.current) dispatch({ type: 'loaded', detail })
    } catch (e) {
      if (!isAbortError(e)) console.warn('[chat] resync failed', e)
    }
  }, [sessionId])

  const retry = useCallback(async (): Promise<void> => {
    if (streamingRef.current || recoveringRef.current) return
    dispatch({ type: 'clear_turn_error' })
    let awaiting = stateRef.current.awaitingReply
    try {
      const detail = await getChat(sessionId)
      dispatch({ type: 'loaded', detail })
      awaiting = detail.awaiting_reply
    } catch {
      /* fall through with what we know */
    }
    if (awaiting) await runTurn({ resume: true })
    else if (lastSentTextRef.current) await runTurn({ text: lastSentTextRef.current })
  }, [sessionId, runTurn])

  /* --------------------------------------------------------------- actions */

  const dispatchAction = useCallback(
    async <A extends ActionType>(cardId: string, action: A, payload: ActionPayloadMap[A]): Promise<ActionResponse | null> => {
      if (actionBusyRef.current || streamingRef.current || recoveringRef.current) return null
      const spec = stateRef.current.spec
      if (!spec) return null
      actionBusyRef.current = true
      epochRef.current++
      dispatch({ type: 'action_begin', cardId, action })
      try {
        const response = await sendAction(sessionId, { card_id: cardId, action, payload, spec_version: spec.spec_version })
        dispatch({ type: 'action_done', response })
        optionsRef.current.onActionSuccess?.(action, response)
        // The server records each click as a transcript line ("Selected drug: adalimumab") that holds the
        // cards it created; quietly pick that up so the page reads the same as after a reload.
        void resync()
        return response
      } catch (e) {
        const kind = e instanceof ChatApiError ? e.kind : 'other'
        const code = e instanceof ChatApiError ? e.code : null
        const message = e instanceof Error ? e.message : 'That did not work. Please try again.'
        if (kind === 'stale_spec') {
          dispatch({ type: 'action_failed', cardId, message: 'This card was out of date, so I refreshed the conversation. Please try again.' })
          optionsRef.current.notify?.('That card was out of date. The conversation has been refreshed.')
          await resync()
        } else if (kind === 'license' || kind === 'rate_limit') {
          const failure = describeFailure(kind, code, message)
          dispatch({ type: 'action_failed', cardId, message: failure.banner?.text ?? message })
          if (failure.banner) dispatch({ type: 'set_banner', banner: failure.banner })
        } else {
          dispatch({ type: 'action_failed', cardId, message })
          // A proposal that went stale, or a card the server no longer treats as open: show the server's truth.
          if (kind === 'conflict' || kind === 'not_found') await resync()
        }
        return null
      } finally {
        actionBusyRef.current = false
      }
    },
    [sessionId, resync],
  )

  /* ------------------------------------------------------------ lifecycle */

  useEffect(() => {
    // Set true in the SETUP, not just via the useRef() initial value: React
    // StrictMode runs setup -> cleanup -> setup on every mount, and the cleanup
    // below sets it false.
    mountedRef.current = true
    const ctrl = new AbortController()
    dispatch({ type: 'load_start' })
    getChat(sessionId, { signal: ctrl.signal })
      .then(detail => {
        if (ctrl.signal.aborted) return
        dispatch({ type: 'loaded', detail })
        if (detail.awaiting_reply && optionsRef.current.autoResume !== false && resumedForRef.current !== sessionId) {
          resumedForRef.current = sessionId
          void runTurn({ resume: true })
        }
      })
      .catch(e => {
        if (ctrl.signal.aborted || isAbortError(e)) return
        dispatch({
          type: 'load_error',
          message: e instanceof Error ? e.message : 'Could not open this conversation.',
          errorKind: e instanceof ChatApiError ? e.kind : null,
        })
      })
    return () => {
      mountedRef.current = false
      ctrl.abort()
      // Stop listening; the server still finishes the turn and saves it.
      abortRef.current?.abort()
      recoverAbortRef.current?.abort()
    }
  }, [sessionId, runTurn])

  const thread = useMemo(() => selectThread(state), [state])
  const busy = state.streaming || state.recovering

  return {
    sessionId,
    state,
    thread,
    busy,
    send,
    stop,
    retry,
    resync,
    dispatchAction,
    dismissBanner: useCallback(() => dispatch({ type: 'set_banner', banner: null }), []),
    dismissTurnError: useCallback(() => dispatch({ type: 'clear_turn_error' }), []),
    clearRejectedText: useCallback(() => dispatch({ type: 'clear_rejected_text' }), []),
  }
}
