import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { ChatStreamEvent } from '../types'
import { DRUG_MATCHES, SESSION_ID, actionResponse, card, detail, message, spec } from './fixtures'

const h = vi.hoisted(() => ({ getChat: vi.fn(), sendAction: vi.fn(), streamTurn: vi.fn() }))

vi.mock('../../api/chat', async importOriginal => {
  const actual = await importOriginal<typeof import('../../api/chat')>()
  return { ...actual, getChat: h.getChat, sendAction: h.sendAction, streamTurn: h.streamTurn }
})

import { ChatApiError } from '../../api/chat'
import ChatPage from '../ChatPage'

function renderPage() {
  return render(
    <MemoryRouter initialEntries={[`/app/chat/${SESSION_ID}`]}>
      <Routes>
        <Route path="/app/chat/:sessionId" element={<ChatPage />} />
        <Route path="*" element={<p>elsewhere</p>} />
      </Routes>
    </MemoryRouter>,
  )
}

const MATCHES = DRUG_MATCHES()
const box = () => screen.getByRole('textbox', { name: 'Message the assistant' }) as HTMLTextAreaElement

function script(events: ChatStreamEvent[]) {
  h.streamTurn.mockImplementation(async (_sid: string, _body: unknown, opts: { onEvent: (e: ChatStreamEvent) => void }) => {
    for (const e of events) opts.onEvent(e)
  })
}

beforeEach(() => {
  h.getChat.mockReset()
  h.sendAction.mockReset()
  h.streamTurn.mockReset()
})

describe('ChatPage', () => {
  it('shows the transcript with each card under the message that produced it', async () => {
    h.getChat.mockResolvedValue(
      detail({
        messages: [
          message({ id: 'u1', seq: 1, role: 'user', text: 'Biosimilar PK study for adalimumab' }),
          message({ id: 'a1', seq: 2, role: 'assistant', text: 'I found **adalimumab** in the catalogue.', card_ids: [MATCHES.id], flags: { unverified_numbers: ['24'] } }),
        ],
        cards: [MATCHES],
      }),
    )
    renderPage()
    expect(await screen.findByText('Biosimilar PK study for adalimumab')).toBeInTheDocument()
    const assistant = screen.getByText(/in the catalogue\./).closest('[data-row]') as HTMLElement
    expect(within(assistant).getByRole('button', { name: 'Use adalimumab' })).toBeInTheDocument()
    expect(within(assistant).getByTestId('unverified-note')).toHaveTextContent('Check the cards for authoritative values.')
    expect(h.streamTurn).not.toHaveBeenCalled()
  })

  it('clicking a card button sends the action with the spec version, then shows what came back', async () => {
    h.getChat.mockResolvedValue(detail({ messages: [message({ id: 'a1', role: 'assistant', text: 'Pick one.', card_ids: [MATCHES.id] })], cards: [MATCHES] }))
    const done = { ...MATCHES, status: 'done' as const }
    const next = card('notice', { level: 'info', text: 'Now choose the study type.', code: null }, { title: 'Next' })
    h.sendAction.mockResolvedValue(actionResponse({ cards: [done, next], guide: { text: 'Which type of study is it?', chips: ['Biosimilar PK study'] } }))
    renderPage()
    await screen.findByRole('button', { name: 'Use adalimumab' })
    // The server records the click as a transcript line holding the card it created.
    h.getChat.mockResolvedValue(
      detail({
        spec: spec({ spec_version: 4 }),
        messages: [
          message({ id: 'a1', seq: 1, role: 'assistant', text: 'Pick one.', card_ids: [MATCHES.id] }),
          message({ id: 'ui1', seq: 2, role: 'ui_action', text: 'Selected drug: adalimumab', card_ids: [next.id] }),
        ],
        cards: [done, next],
      }),
    )
    fireEvent.click(await screen.findByRole('button', { name: 'Use adalimumab' }))
    await waitFor(() => expect(h.sendAction).toHaveBeenCalledTimes(1))
    expect(h.sendAction).toHaveBeenCalledWith(SESSION_ID, {
      card_id: MATCHES.id,
      action: 'select_drug',
      payload: { drug_id: '22222222-2222-4222-8222-222222222222' },
      spec_version: 3,
    })
    expect(await screen.findByText('Now choose the study type.')).toBeInTheDocument()
    expect(await screen.findByText('Selected drug: adalimumab')).toBeInTheDocument()
    expect(screen.getByText('Which type of study is it?')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Biosimilar PK study/ })).toBeInTheDocument()
    // The clicked card is finished and read-only.
    expect(screen.getByRole('button', { name: 'Use adalimumab' })).toBeDisabled()
  })

  it('on 409 stale_spec the page refetches and says so', async () => {
    h.getChat.mockResolvedValue(detail({ messages: [message({ id: 'a1', role: 'assistant', text: 'Pick one.', card_ids: [MATCHES.id] })], cards: [MATCHES] }))
    renderPage()
    const use = await screen.findByRole('button', { name: 'Use adalimumab' })
    h.sendAction.mockRejectedValue(new ChatApiError(409, 'moved on', 'stale_spec', 'stale_spec', null))
    h.getChat.mockResolvedValue(detail({ messages: [message({ id: 'a1', role: 'assistant', text: 'Pick one.', card_ids: [MATCHES.id] })], cards: [{ ...MATCHES, status: 'superseded' }], spec: spec({ spec_version: 7 }) }))
    fireEvent.click(use)
    expect(await screen.findByText(/out of date, so I refreshed the conversation/i)).toBeInTheDocument()
    await waitFor(() => expect(screen.getByRole('button', { name: 'Use adalimumab' })).toBeDisabled())
  })

  it('sends on Enter, streams the reply, announces only the finished message, and shows Stop while streaming', async () => {
    h.getChat.mockResolvedValue(detail())
    let release: () => void = () => {}
    h.streamTurn.mockImplementation(async (_sid: string, _body: unknown, opts: { onEvent: (e: ChatStreamEvent) => void }) => {
      opts.onEvent({ event: 'turn_start', data: { turn_id: 't' } })
      opts.onEvent({ event: 'text_delta', data: { text: 'Checking ' } })
      opts.onEvent({ event: 'tool_start', data: { tool: 'search_drugs', label: 'Checking the DailyMed label…' } })
      await new Promise<void>(resolve => (release = resolve))
      opts.onEvent({ event: 'text_delta', data: { text: 'Done: **adalimumab** is a biologic.' } })
      opts.onEvent({ event: 'turn_end', data: { message_id: 'm9', stop_reason: 'end_turn', flags: { unverified_numbers: [] } } })
    })
    renderPage()
    await waitFor(() => expect(box()).toBeEnabled())
    fireEvent.change(box(), { target: { value: 'adalimumab biosimilar' } })
    fireEvent.keyDown(box(), { key: 'Enter' })

    expect(await screen.findByText('adalimumab biosimilar')).toBeInTheDocument()
    expect(await screen.findByText('Checking the DailyMed label…')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Stop' })).toBeInTheDocument()
    // Streaming text is not announced; the polite region stays empty until the message is finished.
    expect(screen.getByTestId('announcer')).toHaveTextContent('')

    release()
    expect(await within(screen.getByTestId('thread')).findByText(/is a biologic\./)).toBeInTheDocument()
    await waitFor(() => expect(screen.getByTestId('announcer')).toHaveTextContent('Done: adalimumab is a biologic.'))
    expect(screen.queryByRole('button', { name: 'Stop' })).toBeNull()
    expect(h.streamTurn.mock.calls[0][1]).toMatchObject({ text: 'adalimumab biosimilar' })
    // Focus goes back to the composer once the reply has finished.
    await waitFor(() => expect(document.activeElement).toBe(box()))
  })

  it('starts the reply to an unanswered first message by itself', async () => {
    h.getChat.mockResolvedValue(detail({ messages: [message({ id: 'u1', role: 'user', text: 'I want to set up a bioequivalence protocol.' })], awaiting_reply: true }))
    script([
      { event: 'turn_start', data: { turn_id: 't' } },
      { event: 'text_delta', data: { text: 'Which drug is it for?' } },
      { event: 'turn_end', data: { message_id: 'm1', stop_reason: 'end_turn', flags: { unverified_numbers: [] } } },
    ])
    renderPage()
    expect(await within(await screen.findByTestId('thread')).findByText('Which drug is it for?')).toBeInTheDocument()
    expect(h.streamTurn).toHaveBeenCalledTimes(1)
    expect(h.streamTurn.mock.calls[0][1]).toMatchObject({ resume: true })
  })

  it('puts the typed text back and explains when the license is not active', async () => {
    h.getChat.mockResolvedValue(detail())
    h.streamTurn.mockRejectedValue(new ChatApiError(402, 'pending', 'license', null, null))
    renderPage()
    await waitFor(() => expect(box()).toBeEnabled())
    fireEvent.change(box(), { target: { value: 'my study description' } })
    fireEvent.keyDown(box(), { key: 'Enter' })
    expect(await screen.findByText(/license is not active yet, so the assistant is paused/i)).toBeInTheDocument()
    await waitFor(() => expect(box()).toHaveValue('my study description'))
  })

  it('opens the study spec as a sheet below 1280px', async () => {
    h.getChat.mockResolvedValue(detail({ spec: spec({ missing: ['drug'] }) }))
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'Study spec' }))
    const dialog = await screen.findByRole('dialog', { name: 'Study spec' })
    expect(within(dialog).getByText('Still needed')).toBeInTheDocument()
    expect(within(dialog).getByText('FDA-BE-2014')).toBeInTheDocument()
    expect(within(dialog).getByText('Default')).toBeInTheDocument()
  })

  it('says so when the conversation cannot be opened', async () => {
    h.getChat.mockRejectedValue(new ChatApiError(404, 'Not found', 'not_found', 'not_found', null))
    renderPage()
    expect(await screen.findByText('This conversation is not available')).toBeInTheDocument()
  })

  it('shows the kind’s starter suggestions in an empty conversation', async () => {
    h.getChat.mockResolvedValue(detail())
    renderPage()
    expect(await screen.findByText('Set up a study protocol')).toBeInTheDocument()
    const chip = screen.getByRole('button', { name: /Biosimilar PK study of adalimumab/ })
    script([{ event: 'turn_end', data: { message_id: 'm', stop_reason: 'end_turn', flags: { unverified_numbers: [] } } }])
    fireEvent.click(chip)
    await waitFor(() => expect(h.streamTurn).toHaveBeenCalledTimes(1))
    expect(h.streamTurn.mock.calls[0][1]).toMatchObject({ text: 'Biosimilar PK study of adalimumab in healthy volunteers' })
  })
})
