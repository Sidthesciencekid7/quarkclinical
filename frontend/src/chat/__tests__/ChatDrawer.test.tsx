import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { actionResponse, card, detail, message } from './fixtures'
import type { ChatSessionListItem } from '../types'

const h = vi.hoisted(() => ({ listChats: vi.fn(), createChat: vi.fn(), getChat: vi.fn(), sendAction: vi.fn(), streamTurn: vi.fn() }))

vi.mock('../../api/chat', async importOriginal => {
  const actual = await importOriginal<typeof import('../../api/chat')>()
  return { ...actual, listChats: h.listChats, createChat: h.createChat, getChat: h.getChat, sendAction: h.sendAction, streamTurn: h.streamTurn }
})

import { ChatDrawer, findOrCreateEditSession } from '../ChatDrawer'

const P = '33333333-3333-4333-8333-333333333333'
const OTHER = '44444444-4444-4444-8444-444444444444'

function item(over: Partial<ChatSessionListItem> & Pick<ChatSessionListItem, 'id'>): ChatSessionListItem {
  return {
    kind: 'protocol_edit',
    status: 'active',
    title: null,
    protocol_id: P,
    budget_id: null,
    created_at: '2026-10-20T10:00:00Z',
    last_activity_at: '2026-10-20T10:00:00Z',
    ...over,
  }
}

function mount(props: Partial<React.ComponentProps<typeof ChatDrawer>> = {}) {
  const onClose = vi.fn()
  const onApplied = vi.fn()
  const view = render(
    <MemoryRouter>
      <ChatDrawer protocolId={P} open onClose={onClose} onApplied={onApplied} {...props} />
    </MemoryRouter>,
  )
  return { onClose, onApplied, ...view }
}

beforeEach(() => {
  for (const m of Object.values(h)) m.mockReset()
  h.getChat.mockResolvedValue(detail({ session: { ...detail().session, kind: 'protocol_edit', protocol_id: P } }))
})

describe('findOrCreateEditSession', () => {
  it('reuses the most recently active, active edit session for this protocol', async () => {
    h.listChats.mockResolvedValue([
      item({ id: 'old', last_activity_at: '2026-10-01T10:00:00Z' }),
      item({ id: 'other-protocol', protocol_id: OTHER, last_activity_at: '2026-10-23T10:00:00Z' }),
      item({ id: 'archived', status: 'archived', last_activity_at: '2026-10-22T10:00:00Z' }),
      item({ id: 'newest', last_activity_at: '2026-10-21T10:00:00Z' }),
    ])
    await expect(findOrCreateEditSession(P)).resolves.toBe('newest')
    expect(h.listChats).toHaveBeenCalledWith({ kind: 'protocol_edit', limit: 100 })
    expect(h.createChat).not.toHaveBeenCalled()
  })

  it('creates one when there is none', async () => {
    h.listChats.mockResolvedValue([item({ id: 'x', protocol_id: OTHER }), item({ id: 'y', status: 'archived' })])
    h.createChat.mockResolvedValue({ id: 'fresh' })
    await expect(findOrCreateEditSession(P)).resolves.toBe('fresh')
    expect(h.createChat).toHaveBeenCalledWith({ kind: 'protocol_edit', protocol_id: P })
  })
})

describe('ChatDrawer', () => {
  it('does nothing until it is opened', () => {
    mount({ open: false })
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(h.listChats).not.toHaveBeenCalled()
  })

  it('opens the existing edit conversation for the protocol, as a labelled modal dialog', async () => {
    h.listChats.mockResolvedValue([item({ id: 'edit-1' })])
    mount()
    const dialog = screen.getByRole('dialog', { name: 'Edit with AI' })
    expect(dialog).toHaveAttribute('aria-modal', 'true')
    await waitFor(() => expect(h.getChat).toHaveBeenCalledWith('edit-1', expect.anything()))
    expect(h.createChat).not.toHaveBeenCalled()
    expect(await screen.findByRole('textbox', { name: 'Message the assistant' })).toBeInTheDocument()
  })

  it('creates the conversation when there is none, and looks it up only once', async () => {
    h.listChats.mockResolvedValue([])
    h.createChat.mockResolvedValue({ id: 'new-edit' })
    const { rerender, onClose, onApplied } = mount()
    await waitFor(() => expect(h.getChat).toHaveBeenCalledWith('new-edit', expect.anything()))
    expect(h.createChat).toHaveBeenCalledTimes(1)
    expect(h.createChat).toHaveBeenCalledWith({ kind: 'protocol_edit', protocol_id: P })
    // Closing and reopening reuses it: no second lookup.
    rerender(
      <MemoryRouter>
        <ChatDrawer protocolId={P} open={false} onClose={onClose} onApplied={onApplied} />
      </MemoryRouter>,
    )
    rerender(
      <MemoryRouter>
        <ChatDrawer protocolId={P} open onClose={onClose} onApplied={onApplied} />
      </MemoryRouter>,
    )
    await waitFor(() => expect(screen.getByRole('dialog')).toBeInTheDocument())
    expect(h.listChats).toHaveBeenCalledTimes(1)
    expect(h.createChat).toHaveBeenCalledTimes(1)
  })

  it('calls onApplied after an edit is applied, and only then', async () => {
    h.listChats.mockResolvedValue([item({ id: 'edit-1' })])
    const PID = '55555555-5555-4555-8555-555555555555'
    const diff = card('edit_diff', { proposal_id: PID, kind: 'prose', status: 'ready', base_version: 2, stale: false, sections: [], qc: null, facts_before: [], facts_after: [], applied_version: null }, { actions: [{ id: 'apply_edit', label: 'Apply to protocol', variant: 'primary' }, { id: 'discard_edit', label: 'Discard', variant: 'ghost' }] })
    h.getChat.mockResolvedValue(detail({ session: { ...detail().session, kind: 'protocol_edit', protocol_id: P }, messages: [message({ id: 'a', role: 'assistant', text: 'Here is the change.', card_ids: [diff.id] })], cards: [diff] }))
    const { onApplied } = mount()

    // A failed apply does not count.
    h.sendAction.mockRejectedValueOnce(new Error('The protocol changed.'))
    fireEvent.click(await screen.findByRole('button', { name: 'Apply to protocol' }))
    await waitFor(() => expect(h.sendAction).toHaveBeenCalledTimes(1))
    expect(onApplied).not.toHaveBeenCalled()

    h.sendAction.mockResolvedValueOnce(actionResponse({ cards: [{ ...diff, status: 'done', data: { ...diff.data, status: 'applied', applied_version: 3 } }] }))
    fireEvent.click(await screen.findByRole('button', { name: 'Apply to protocol' }))
    await waitFor(() => expect(onApplied).toHaveBeenCalledTimes(1))
    expect(h.sendAction.mock.calls[1][1]).toMatchObject({ card_id: diff.id, action: 'apply_edit', payload: { proposal_id: PID } })
  })

  it('closes with Escape and with the close button, and moves focus into the dialog', async () => {
    h.listChats.mockResolvedValue([item({ id: 'edit-1' })])
    const { onClose } = mount()
    const dialog = screen.getByRole('dialog', { name: 'Edit with AI' })
    await waitFor(() => expect(dialog.contains(document.activeElement)).toBe(true))
    fireEvent.keyDown(dialog, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByRole('button', { name: 'Close editing assistant' }))
    expect(onClose).toHaveBeenCalledTimes(2)
  })

  it('offers a retry when the lookup fails', async () => {
    h.listChats.mockRejectedValueOnce(new Error('Network down'))
    h.listChats.mockResolvedValueOnce([item({ id: 'edit-1' })])
    mount()
    expect(await screen.findByText('Network down')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    await waitFor(() => expect(h.getChat).toHaveBeenCalledWith('edit-1', expect.anything()))
  })
})
