import { act, renderHook, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { ChatStreamEvent } from '../types'
import { actionResponse, card, detail, message, spec, DRUG_MATCHES, SESSION_ID } from './fixtures'

const h = vi.hoisted(() => ({
  getChat: vi.fn(),
  sendAction: vi.fn(),
  streamTurn: vi.fn(),
}))

vi.mock('../../api/chat', async importOriginal => {
  const actual = await importOriginal<typeof import('../../api/chat')>()
  return { ...actual, getChat: h.getChat, sendAction: h.sendAction, streamTurn: h.streamTurn }
})

import { ChatApiError } from '../../api/chat'
import { chatReducer, initialChatState, selectThread } from '../chatReducer'
import type { ChatState } from '../chatReducer'
import { useChatSession } from '../useChatSession'

const ev = <E extends ChatStreamEvent>(e: E): E => e
const NOW = '2026-10-24T10:00:00Z'

function reduce(events: ChatStreamEvent[], start?: ChatState): ChatState {
  let s = chatReducer(start ?? chatReducer(initialChatState, { type: 'loaded', detail: detail() }), { type: 'turn_begin', localId: 'x', userText: 'hello', now: NOW })
  for (const event of events) s = chatReducer(s, { type: 'stream_event', event, now: NOW })
  return s
}

const MATCHES = DRUG_MATCHES()

const SCRIPT: ChatStreamEvent[] = [
  ev({ event: 'turn_start', data: { turn_id: 't1' } }),
  ev({ event: 'text_delta', data: { text: 'Let me ' } }),
  ev({ event: 'text_delta', data: { text: 'look that up.' } }),
  ev({ event: 'tool_start', data: { tool: 'search_drugs', label: 'Searching the drug catalogue…' } }),
  ev({ event: 'card', data: MATCHES }),
  ev({ event: 'spec', data: { spec_version: 4, summary: spec({ spec_version: 4 }) } }),
  ev({ event: 'text_delta', data: { text: 'I found **adalimumab**.' } }),
  ev({ event: 'turn_end', data: { message_id: 'm-final', stop_reason: 'end_turn', flags: { unverified_numbers: ['24'] } } }),
]

describe('chatReducer: a scripted turn', () => {
  it('streams text, shows the tool label, upserts cards by id and finalises on turn_end', () => {
    // Mid-turn: the tool label is shown and the first segment is sealed.
    const mid = reduce(SCRIPT.slice(0, 4))
    expect(mid.streaming).toBe(true)
    expect(mid.toolLabel).toBe('Searching the drug catalogue…')
    expect(mid.segments).toHaveLength(1)
    expect(mid.segments[0].text).toBe('Let me look that up.')

    const done = reduce(SCRIPT)
    expect(done.streaming).toBe(false)
    expect(done.toolLabel).toBeNull()
    expect(done.segments).toEqual([])
    expect(done.spec?.spec_version).toBe(4)
    expect(done.session?.spec_version).toBe(4)
    expect(done.announcement).toBe('I found adalimumab.')

    const messages = done.entries.flatMap(e => (e.kind === 'message' ? [e.message] : []))
    expect(messages.map(m => [m.role, m.text])).toEqual([
      ['user', 'hello'],
      ['assistant', 'Let me look that up.'],
      ['assistant', 'I found **adalimumab**.'],
    ])
    // The cards belong to the message that issued the tool call, not the final one.
    expect(messages[1].card_ids).toEqual([MATCHES.id])
    expect(messages[2].card_ids).toEqual([])
    // The final message takes the server's id and the turn's flags.
    expect(messages[2].id).toBe('m-final')
    expect(messages[2].flags.unverified_numbers).toEqual(['24'])
    expect(messages[1].flags.unverified_numbers).toEqual([])
  })

  it('upserts a card with an existing id in place (no duplicate, no new placement)', () => {
    const updated = { ...MATCHES, status: 'superseded' as const }
    const s = reduce([...SCRIPT.slice(0, 5), ev({ event: 'card', data: updated })])
    expect(s.cards.filter(c => c.id === MATCHES.id)).toHaveLength(1)
    expect(s.cards.find(c => c.id === MATCHES.id)?.status).toBe('superseded')
    expect(s.segments[0].cardIds).toEqual([MATCHES.id])
  })

  it('renders cards under the message that produced them', () => {
    const rows = selectThread(reduce(SCRIPT))
    expect(rows.map(r => r.kind)).toEqual(['user', 'assistant', 'assistant'])
    expect(rows[1].cards.map(c => c.id)).toEqual([MATCHES.id])
    expect(rows[2].cards).toEqual([])
  })

  it('keeps partial text and offers a retry when the stream reports an error', () => {
    const s = reduce([
      ev({ event: 'turn_start', data: { turn_id: 't' } }),
      ev({ event: 'text_delta', data: { text: 'Working on' } }),
      ev({ event: 'error', data: { code: 'model_error', message: 'The assistant is busy right now.', retryable: true } }),
    ])
    expect(s.streaming).toBe(false)
    expect(s.turnError).toEqual({ code: 'model_error', message: 'The assistant is busy right now.', retryable: true })
    expect(selectThread(s).some(r => r.kind === 'assistant' && r.text === 'Working on')).toBe(true)
  })

  it('turns a license error event into a banner instead of an error line', () => {
    const s = reduce([ev({ event: 'error', data: { code: 'license_required', message: 'x', retryable: false } })])
    expect(s.turnError).toBeNull()
    expect(s.banner?.kind).toBe('license')
    expect(s.banner?.text).toMatch(/license is not active yet/i)
  })

  it('records a notice event as a thread line', () => {
    const s = reduce([
      ev({ event: 'text_delta', data: { text: 'Hi' } }),
      ev({ event: 'notice', data: { level: 'info', text: 'That reply was cut off.' } }),
      ev({ event: 'turn_end', data: { message_id: 'm', stop_reason: 'max_tokens', flags: { unverified_numbers: [] } } }),
    ])
    expect(selectThread(s).map(r => r.kind)).toEqual(['user', 'assistant', 'notice'])
  })

  it('shows a card an action created after the last message, and keeps it there on the next turn', () => {
    const loaded = chatReducer(initialChatState, {
      type: 'loaded',
      detail: detail({ messages: [message({ id: 'm1', role: 'assistant', text: 'Pick one.' })] }),
    })
    const withCard = chatReducer(loaded, { type: 'action_done', response: actionResponse({ cards: [MATCHES] }) })
    expect(selectThread(withCard).map(r => r.kind)).toEqual(['assistant', 'cards'])
    const next = chatReducer(withCard, { type: 'turn_begin', localId: 'n', userText: 'and then?', now: NOW })
    // Pinned to the message it followed: it stays above the new question.
    const rows = selectThread(next)
    expect(rows.map(r => r.kind)).toEqual(['assistant', 'user'])
    expect(rows[0].cards.map(c => c.id)).toEqual([MATCHES.id])
  })
})

/* ------------------------------------------------------------------ the hook */

function script(events: ChatStreamEvent[]) {
  h.streamTurn.mockImplementation(async (_sid: string, _body: unknown, opts: { onEvent: (e: ChatStreamEvent) => void }) => {
    for (const e of events) opts.onEvent(e)
  })
}

beforeEach(() => {
  h.getChat.mockReset()
  h.sendAction.mockReset()
  h.streamTurn.mockReset()
  h.getChat.mockResolvedValue(detail())
})

describe('useChatSession', () => {
  it('loads the conversation and shows it', async () => {
    h.getChat.mockResolvedValue(detail({ messages: [message({ id: 'm1', role: 'assistant', text: 'Hello' })] }))
    const { result } = renderHook(() => useChatSession(SESSION_ID))
    await waitFor(() => expect(result.current.state.load).toBe('ready'))
    expect(result.current.thread.map(r => r.text)).toEqual(['Hello'])
    expect(h.streamTurn).not.toHaveBeenCalled()
  })

  it('runs a resume turn exactly once when the conversation is awaiting a reply', async () => {
    h.getChat.mockResolvedValue(detail({ messages: [message({ id: 'u1', role: 'user', text: 'Biosimilar PK study' })], awaiting_reply: true }))
    script(SCRIPT.filter(e => e.event !== 'card'))
    const { result } = renderHook(() => useChatSession(SESSION_ID))
    await waitFor(() => expect(h.streamTurn).toHaveBeenCalledTimes(1))
    const [sid, body] = h.streamTurn.mock.calls[0]
    expect(sid).toBe(SESSION_ID)
    expect(body).toMatchObject({ resume: true })
    expect(body.text).toBeUndefined()
    expect(body.client_turn_id).toMatch(/^[A-Za-z0-9._:-]{1,64}$/)
    await waitFor(() => expect(result.current.state.streaming).toBe(false))
    expect(result.current.thread.map(r => r.kind)).toEqual(['user', 'assistant', 'assistant'])
    expect(h.streamTurn).toHaveBeenCalledTimes(1)
  })

  it('sends typed text with a fresh client_turn_id each time, and shows it optimistically', async () => {
    const { result } = renderHook(() => useChatSession(SESSION_ID))
    await waitFor(() => expect(result.current.state.load).toBe('ready'))
    script([ev({ event: 'turn_start', data: { turn_id: 't' } }), ev({ event: 'text_delta', data: { text: 'Sure.' } }), ev({ event: 'turn_end', data: { message_id: 'm2', stop_reason: 'end_turn', flags: { unverified_numbers: [] } } })])
    await act(async () => {
      await result.current.send('  make it a BE study  ')
    })
    await act(async () => {
      await result.current.send('and food effect')
    })
    const bodies = h.streamTurn.mock.calls.map(c => c[1])
    expect(bodies[0]).toMatchObject({ text: 'make it a BE study' })
    expect(bodies[1]).toMatchObject({ text: 'and food effect' })
    expect(bodies[0].client_turn_id).not.toBe(bodies[1].client_turn_id)
    expect(result.current.thread.filter(r => r.kind === 'user').map(r => r.text)).toEqual(['make it a BE study', 'and food effect'])
    expect(result.current.state.announcement).toBe('Sure.')
  })

  it('on 409 busy removes the optimistic message, hands the text back and shows the busy banner', async () => {
    const { result, unmount } = renderHook(() => useChatSession(SESSION_ID))
    await waitFor(() => expect(result.current.state.load).toBe('ready'))
    h.streamTurn.mockRejectedValue(new ChatApiError(409, 'Another reply is still running', 'busy', 'busy', null))
    await act(async () => {
      await result.current.send('hello again')
    })
    expect(result.current.state.banner?.kind).toBe('busy')
    expect(result.current.state.banner?.text).toMatch(/still running/i)
    expect(result.current.state.rejectedText).toBe('hello again')
    expect(result.current.thread.filter(r => r.kind === 'user')).toHaveLength(0)
    unmount() // stops the quiet catch-up polling
  })

  it('on 402 shows the pending-license explanation and keeps the text', async () => {
    const { result } = renderHook(() => useChatSession(SESSION_ID))
    await waitFor(() => expect(result.current.state.load).toBe('ready'))
    h.streamTurn.mockRejectedValue(new ChatApiError(402, 'License is pending', 'license', null, null))
    await act(async () => {
      await result.current.send('draft text')
    })
    expect(result.current.state.banner).toMatchObject({ kind: 'license', title: 'License not active yet' })
    expect(result.current.state.rejectedText).toBe('draft text')
  })

  it('on 429 shows the limit that was hit', async () => {
    const { result } = renderHook(() => useChatSession(SESSION_ID))
    await waitFor(() => expect(result.current.state.load).toBe('ready'))
    h.streamTurn.mockRejectedValue(new ChatApiError(429, 'Too many', 'rate_limit', 'session_cap', null))
    await act(async () => {
      await result.current.send('one more')
    })
    expect(result.current.state.banner).toMatchObject({ kind: 'rate', title: 'This conversation is full' })
  })

  it('syncs from the server after the stream is interrupted', async () => {
    const { result, unmount } = renderHook(() => useChatSession(SESSION_ID))
    await waitFor(() => expect(result.current.state.load).toBe('ready'))
    const calls = h.getChat.mock.calls.length
    h.getChat.mockResolvedValue(detail({ messages: [message({ id: 'u9', role: 'user', text: 'hi' }), message({ id: 'a9', role: 'assistant', seq: 2, text: 'The saved reply' })] }))
    script([ev({ event: 'text_delta', data: { text: 'The sav' } }), ev({ event: 'error', data: { code: 'stream_interrupted', message: 'closed', retryable: true } })])
    await act(async () => {
      await result.current.send('hi')
    })
    await waitFor(() => expect(h.getChat.mock.calls.length).toBeGreaterThan(calls))
    await waitFor(() => expect(result.current.thread.some(r => r.text === 'The saved reply')).toBe(true))
    unmount()
  })

  it('aborts the stream when the page is left', async () => {
    let signal: AbortSignal | undefined
    h.streamTurn.mockImplementation(async (_s: string, _b: unknown, opts: { signal: AbortSignal }) => {
      signal = opts.signal
      await new Promise<void>(resolve => opts.signal.addEventListener('abort', () => resolve()))
    })
    const { result, unmount } = renderHook(() => useChatSession(SESSION_ID))
    await waitFor(() => expect(result.current.state.load).toBe('ready'))
    void act(() => {
      void result.current.send('long one')
    })
    await waitFor(() => expect(signal).toBeDefined())
    expect(signal!.aborted).toBe(false)
    unmount()
    expect(signal!.aborted).toBe(true)
  })

  it('Stop aborts the read but not the conversation', async () => {
    h.streamTurn.mockImplementation(async (_s: string, _b: unknown, opts: { signal: AbortSignal; onEvent: (e: ChatStreamEvent) => void }) => {
      opts.onEvent(ev({ event: 'text_delta', data: { text: 'Partial answer' } }))
      await new Promise<void>(resolve => opts.signal.addEventListener('abort', () => resolve()))
    })
    const { result, unmount } = renderHook(() => useChatSession(SESSION_ID))
    await waitFor(() => expect(result.current.state.load).toBe('ready'))
    act(() => {
      void result.current.send('go')
    })
    await waitFor(() => expect(result.current.state.segments[0]?.text).toBe('Partial answer'))
    act(() => result.current.stop())
    await waitFor(() => expect(result.current.state.streaming).toBe(false))
    expect(result.current.thread.some(r => r.text === 'Partial answer')).toBe(true)
    unmount()
  })
})

describe('useChatSession: card actions', () => {
  const MATCH_ID = '22222222-2222-4222-8222-222222222222'

  async function ready(notify = vi.fn(), onActionSuccess = vi.fn()) {
    h.getChat.mockResolvedValue(detail({ cards: [MATCHES], spec: spec({ spec_version: 3 }) }))
    const hook = renderHook(() => useChatSession(SESSION_ID, { notify, onActionSuccess }))
    await waitFor(() => expect(hook.result.current.state.load).toBe('ready'))
    return { ...hook, notify, onActionSuccess }
  }

  it('sends the card id, action, payload and the spec version the client last saw', async () => {
    const { result, onActionSuccess } = await ready()
    const done = { ...MATCHES, status: 'done' as const }
    const next = card('study_type', { options: [], selected: null, drug_inn: 'adalimumab', modality: 'biologic', gate_message: null })
    h.sendAction.mockResolvedValue(actionResponse({ cards: [done, next] }))
    // What the server will say when the page quietly refetches after the click.
    h.getChat.mockResolvedValue(
      detail({
        cards: [done, next],
        spec: spec({ spec_version: 4 }),
        messages: [message({ id: 'ui1', role: 'ui_action', text: 'Selected drug: adalimumab', card_ids: [next.id] })],
      }),
    )
    await act(async () => {
      await result.current.dispatchAction(MATCHES.id, 'select_drug', { drug_id: MATCH_ID })
    })
    expect(h.sendAction).toHaveBeenCalledWith(SESSION_ID, { card_id: MATCHES.id, action: 'select_drug', payload: { drug_id: MATCH_ID }, spec_version: 3 })
    // The response is merged: card upserted, new card added, spec and guide updated.
    expect(result.current.state.cards.find(c => c.id === MATCHES.id)?.status).toBe('done')
    expect(result.current.state.cards.some(c => c.type === 'study_type')).toBe(true)
    expect(result.current.state.spec?.spec_version).toBe(4)
    expect(result.current.state.guide?.text).toBe('Next: pick the study type.')
    expect(onActionSuccess).toHaveBeenCalledWith('select_drug', expect.objectContaining({ spec_version: 4 }))
    // The click is also read back as a transcript line that holds the cards it created.
    await waitFor(() => expect(result.current.thread.map(r => [r.kind, r.text])).toContainEqual(['note', 'Selected drug: adalimumab']))
    expect(result.current.thread.find(r => r.kind === 'note')?.cards.map(c => c.type)).toEqual(['study_type'])
    // The next click uses the NEW version.
    h.sendAction.mockResolvedValue(actionResponse({ spec: spec({ spec_version: 5 }) }))
    h.getChat.mockResolvedValue(detail({ cards: [done, next], spec: spec({ spec_version: 5 }) }))
    await act(async () => {
      await result.current.dispatchAction(MATCHES.id, 'set_study_type', { study_type: 'biosimilar_pk' })
    })
    expect(h.sendAction.mock.calls[1][1]).toMatchObject({ spec_version: 4 })
  })

  it('on 409 stale_spec refetches the conversation and tells the user', async () => {
    const { result, notify } = await ready()
    const before = h.getChat.mock.calls.length
    h.sendAction.mockRejectedValue(new ChatApiError(409, 'The spec moved on', 'stale_spec', 'stale_spec', null))
    h.getChat.mockResolvedValue(detail({ cards: [{ ...MATCHES, status: 'superseded' }], spec: spec({ spec_version: 6 }) }))
    await act(async () => {
      await result.current.dispatchAction(MATCHES.id, 'select_drug', { drug_id: MATCH_ID })
    })
    expect(h.getChat.mock.calls.length).toBe(before + 1)
    expect(notify).toHaveBeenCalledWith(expect.stringMatching(/out of date/i))
    expect(result.current.state.spec?.spec_version).toBe(6)
    expect(result.current.state.cards[0].status).toBe('superseded')
    expect(result.current.state.pendingAction).toBeNull()
    expect(result.current.state.cardErrors[MATCHES.id]).toMatch(/out of date/i)
  })

  it('shows a validation refusal on the card, in the server’s words', async () => {
    const { result } = await ready()
    h.sendAction.mockRejectedValue(new ChatApiError(422, 'Biologics cannot run a BE study.', 'validation', 'validation', null))
    await act(async () => {
      await result.current.dispatchAction(MATCHES.id, 'select_drug', { drug_id: MATCH_ID })
    })
    expect(result.current.state.cardErrors[MATCHES.id]).toBe('Biologics cannot run a BE study.')
  })

  it('does not send an action while a reply is streaming', async () => {
    const { result } = await ready()
    h.streamTurn.mockImplementation(async (_s: string, _b: unknown, opts: { signal: AbortSignal }) => {
      await new Promise<void>(resolve => opts.signal.addEventListener('abort', () => resolve()))
    })
    act(() => {
      void result.current.send('go')
    })
    await waitFor(() => expect(result.current.state.streaming).toBe(true))
    await act(async () => {
      await result.current.dispatchAction(MATCHES.id, 'select_drug', { drug_id: MATCH_ID })
    })
    expect(h.sendAction).not.toHaveBeenCalled()
    act(() => result.current.stop())
  })
})
