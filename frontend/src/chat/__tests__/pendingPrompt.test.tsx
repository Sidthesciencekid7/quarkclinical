import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  PENDING_PROMPT_KEY,
  PENDING_PROMPT_MAX_CHARS,
  PENDING_PROMPT_TTL_MS,
  clearPendingPrompt,
  loadPendingPrompt,
  sanitizePendingPromptText,
  savePendingPrompt,
} from '../../lib/pendingPrompt'
import { HOME_DRAFT_KEY, saveHomeDraft } from '../../shell/homeDraft'

const h = vi.hoisted(() => ({ createChat: vi.fn() }))
vi.mock('../../api/chat', () => ({ createChat: h.createChat }))

import NewChatPage, { planNewChat } from '../NewChatPage'

const STUDY = 'Biosimilar PK study of adalimumab in healthy volunteers'

beforeEach(() => {
  window.localStorage.clear()
  window.sessionStorage.clear()
  h.createChat.mockReset()
})
afterEach(() => {
  vi.useRealTimers()
})

describe('pending prompt storage', () => {
  it('round-trips a prompt and does not clear it on load', () => {
    expect(savePendingPrompt({ text: STUDY, kind: 'protocol' })).toBe(true)
    expect(loadPendingPrompt()).toMatchObject({ text: STUDY, kind: 'protocol' })
    expect(loadPendingPrompt()).not.toBeNull()
    clearPendingPrompt()
    expect(loadPendingPrompt()).toBeNull()
  })

  it('strips control characters, keeps newlines and caps the length', () => {
    const cleaned = sanitizePendingPromptText(`  a\u0000b\u0007c\r\nline two\t\u009Fend  `)
    expect(cleaned).toBe('abc\nline twoend')
    expect(Array.from(sanitizePendingPromptText('x'.repeat(PENDING_PROMPT_MAX_CHARS + 500)))).toHaveLength(PENDING_PROMPT_MAX_CHARS)
  })

  it('expires after seven days and forgets the expired value', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-10-01T10:00:00Z'))
    savePendingPrompt({ text: STUDY, kind: 'budget' })
    vi.setSystemTime(new Date(Date.now() + PENDING_PROMPT_TTL_MS - 1000))
    expect(loadPendingPrompt()).not.toBeNull()
    vi.setSystemTime(new Date(Date.now() + 2000))
    expect(loadPendingPrompt()).toBeNull()
    expect(window.localStorage.getItem(PENDING_PROMPT_KEY)).toBeNull()
  })

  it('treats a hand-edited or corrupt value as no prompt', () => {
    window.localStorage.setItem(PENDING_PROMPT_KEY, '{not json')
    expect(loadPendingPrompt()).toBeNull()
    window.localStorage.setItem(PENDING_PROMPT_KEY, JSON.stringify({ v: 1, text: 'x', kind: 'bogus', createdAt: Date.now() }))
    expect(loadPendingPrompt()).toBeNull()
    window.localStorage.setItem(PENDING_PROMPT_KEY, JSON.stringify({ v: 1, text: '\u0000\u0001', kind: 'protocol', createdAt: Date.now() }))
    expect(loadPendingPrompt()).toBeNull()
  })

  it('does not save text that is empty after sanitising', () => {
    expect(savePendingPrompt({ text: '  \u0000  ', kind: 'protocol' })).toBe(false)
    expect(window.localStorage.getItem(PENDING_PROMPT_KEY)).toBeNull()
  })
})

describe('planNewChat', () => {
  it('takes the landing prompt (and its kind) for from=landing', () => {
    savePendingPrompt({ text: STUDY, kind: 'budget' })
    expect(planNewChat({ kind: null, preset: null, from: 'landing', protocolId: null })).toEqual({
      kind: 'budget',
      initialMessage: STUDY,
      protocolId: null,
      clear: 'landing',
    })
  })

  it('takes the Home draft for from=home', () => {
    saveHomeDraft({ text: 'Price a study for nivolumab', kind: 'budget' })
    expect(planNewChat({ kind: 'budget', preset: null, from: 'home', protocolId: null })).toMatchObject({
      kind: 'budget',
      initialMessage: 'Price a study for nivolumab',
      clear: 'home',
    })
    expect(window.sessionStorage.getItem(HOME_DRAFT_KEY)).not.toBeNull()
  })

  it('maps a preset to its starter sentence and ignores unknown presets', () => {
    const be = planNewChat({ kind: 'protocol', preset: 'be', from: null, protocolId: null })
    expect(be).toMatchObject({ kind: 'protocol', clear: null })
    expect(be?.initialMessage).toMatch(/bioequivalence protocol/i)
    expect(planNewChat({ kind: null, preset: 'price', from: null, protocolId: null })?.kind).toBe('budget')
    expect(planNewChat({ kind: null, preset: '__proto__', from: null, protocolId: null })).toBeNull()
  })

  it('prices or edits a given protocol, and shows the start screen for a bare URL', () => {
    expect(planNewChat({ kind: 'budget', preset: null, from: null, protocolId: 'p1' })).toMatchObject({ kind: 'budget', protocolId: 'p1' })
    expect(planNewChat({ kind: 'protocol_edit', preset: null, from: null, protocolId: 'p1' })).toMatchObject({ kind: 'protocol_edit', initialMessage: null })
    expect(planNewChat({ kind: null, preset: null, from: null, protocolId: null })).toBeNull()
    expect(planNewChat({ kind: 'protocol', preset: null, from: 'landing', protocolId: null })).toBeNull()
  })
})

function Where() {
  const loc = useLocation()
  return <p data-testid="where">{`${loc.pathname}${loc.search}`}</p>
}

function renderNew(url: string) {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <Routes>
        <Route path="/app/chat/new" element={<NewChatPage />} />
        <Route path="*" element={<Where />} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('NewChatPage with a saved landing prompt', () => {
  it('creates the session with the text as the first message, then clears the stored prompt', async () => {
    savePendingPrompt({ text: STUDY, kind: 'protocol' })
    h.createChat.mockResolvedValue({ id: 'sess-1' })
    renderNew('/app/chat/new?from=landing&kind=protocol')
    await waitFor(() => expect(screen.getByTestId('where')).toHaveTextContent('/app/chat/sess-1'))
    expect(h.createChat).toHaveBeenCalledTimes(1)
    expect(h.createChat).toHaveBeenCalledWith({ kind: 'protocol', initial_message: STUDY })
    expect(loadPendingPrompt()).toBeNull()
  })

  it('keeps the prompt when createChat fails, and clears it only after a retry succeeds', async () => {
    savePendingPrompt({ text: STUDY, kind: 'protocol' })
    h.createChat.mockRejectedValueOnce(new Error('Server unavailable'))
    renderNew('/app/chat/new?from=landing&kind=protocol')

    expect(await screen.findByText('Server unavailable')).toBeInTheDocument()
    expect(loadPendingPrompt()?.text).toBe(STUDY)

    h.createChat.mockResolvedValueOnce({ id: 'sess-2' })
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    await waitFor(() => expect(screen.getByTestId('where')).toHaveTextContent('/app/chat/sess-2'))
    expect(h.createChat).toHaveBeenCalledTimes(2)
    expect(loadPendingPrompt()).toBeNull()
  })

  it('shows the start screen (and creates nothing) when the saved text is gone', () => {
    renderNew('/app/chat/new?from=landing')
    expect(h.createChat).not.toHaveBeenCalled()
    expect(screen.getByRole('textbox', { name: 'Describe your study' })).toBeInTheDocument()
    expect(screen.getByText(/no longer available in this browser/i)).toBeInTheDocument()
  })

  it('never puts a protocol id on a protocol conversation', async () => {
    h.createChat.mockResolvedValue({ id: 'sess-3' })
    renderNew('/app/chat/new?kind=protocol&preset=be&protocol_id=abc')
    await waitFor(() => expect(h.createChat).toHaveBeenCalledTimes(1))
    expect(h.createChat.mock.calls[0][0]).not.toHaveProperty('protocol_id')
  })
})
