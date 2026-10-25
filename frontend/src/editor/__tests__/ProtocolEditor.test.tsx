import { fireEvent, render, screen, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { Drug, GeneratedProtocol } from '../../types'

// The editor body (TipTap), the review panels and the chat drawer are exercised
// elsewhere; here they are stubs so the toolbar can be tested on its own.
vi.mock('../SectionEditor', () => ({ default: () => <div data-testid="section-editor" /> }))
vi.mock('../QCPanel', () => ({ default: () => <aside aria-label="QC panel stub" /> }))
vi.mock('../CoveragePanel', () => ({ default: () => <aside aria-label="Coverage panel stub" /> }))
vi.mock('../RiskPanel', () => ({ default: () => <aside aria-label="Risk panel stub" /> }))
vi.mock('../CommentPanel', () => ({ default: () => <aside aria-label="Notes panel stub" /> }))

const chat = vi.hoisted(() => ({ lastProps: null as null | { protocolId: string; onApplied?: () => void } }))
vi.mock('../../chat', () => ({
  ChatDrawer: (props: { protocolId: string; open: boolean; onClose: () => void; onApplied?: () => void }) => {
    chat.lastProps = props
    return props.open ? (
      <div role="dialog" aria-label="Edit with AI">
        <button onClick={props.onClose}>Close drawer</button>
      </div>
    ) : null
  },
}))

const caps = vi.hoisted(() => ({ value: { role: 'generator', is_org_owner: true, is_admin: false, can_generate: true } }))
vi.mock('../../lib/useCapabilities', () => ({
  useCapabilities: () => ({ capabilities: caps.value, loading: false, fromServer: true, error: null }),
}))

vi.mock('../../api/client', () => {
  class HttpError extends Error {
    status: number
    constructor(status: number, message: string) {
      super(message)
      this.status = status
    }
  }
  return { HttpError, api: new Proxy({}, { get: () => vi.fn() }) }
})

import ProtocolEditor from '../ProtocolEditor'

const DRUG: Drug = { id: 'd1', inn: 'adalimumab', brand_names: [], rxnorm_id: null, updated_at: '2026-10-01T10:00:00Z' }

function protocol(over: Partial<GeneratedProtocol> = {}): GeneratedProtocol {
  return {
    id: 'p1',
    drug_id: 'd1',
    org_id: 'o1',
    study_type: 'biosimilar_pk',
    population_config: {},
    sections: { synopsis: '<p>Draft text</p>' },
    version: 3,
    status: 'draft',
    guideline_version: 'FDA-BE-2014',
    created_by: 'u1',
    created_at: '2026-10-01T10:00:00Z',
    ...over,
  }
}

function mount(over: Partial<GeneratedProtocol> = {}, onReload?: () => Promise<void>) {
  return render(<ProtocolEditor protocol={protocol(over)} drug={DRUG} onBack={vi.fn()} onReload={onReload} />)
}

beforeEach(() => {
  caps.value = { role: 'generator', is_org_owner: true, is_admin: false, can_generate: true }
  chat.lastProps = null
})

describe('ProtocolEditor toolbar', () => {
  it('shows the title with version and status badges in a light toolbar', () => {
    mount()
    expect(screen.getByRole('heading', { level: 1, name: 'adalimumab' })).toBeInTheDocument()
    expect(screen.getByText('Draft')).toBeInTheDocument()
    expect(screen.getByText(/v3/)).toBeInTheDocument()
  })

  it('exposes QC / Coverage / Risk / Notes as a pill toggle group with aria-pressed', () => {
    mount()
    const group = screen.getByRole('group', { name: 'Review panels' })
    const names = ['QC', 'Coverage', 'Risk', 'Notes']
    for (const n of names) {
      expect(within(group).getByRole('button', { name: n })).toHaveAttribute('aria-pressed', 'false')
    }

    fireEvent.click(within(group).getByRole('button', { name: 'QC' }))
    expect(within(group).getByRole('button', { name: 'QC' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByLabelText('QC panel stub')).toBeInTheDocument()

    // Panels are mutually exclusive: opening Coverage closes QC.
    fireEvent.click(within(group).getByRole('button', { name: 'Coverage' }))
    expect(within(group).getByRole('button', { name: 'Coverage' })).toHaveAttribute('aria-pressed', 'true')
    expect(within(group).getByRole('button', { name: 'QC' })).toHaveAttribute('aria-pressed', 'false')
    expect(screen.queryByLabelText('QC panel stub')).toBeNull()
    expect(screen.getByLabelText('Coverage panel stub')).toBeInTheDocument()

    // Clicking the active pill closes the panel again.
    fireEvent.click(within(group).getByRole('button', { name: 'Coverage' }))
    for (const n of names) {
      expect(within(group).getByRole('button', { name: n })).toHaveAttribute('aria-pressed', 'false')
    }
    expect(screen.queryByLabelText('Coverage panel stub')).toBeNull()
  })

  it('opens each of the four panels', () => {
    mount()
    for (const [name, stub] of [['Risk', 'Risk panel stub'], ['Notes', 'Notes panel stub']] as const) {
      fireEvent.click(screen.getByRole('button', { name }))
      expect(screen.getByLabelText(stub)).toBeInTheDocument()
    }
  })

  it('does not render its own <main> landmark (the shell owns it)', () => {
    const { container } = mount()
    expect(container.querySelector('main')).toBeNull()
    expect(screen.queryAllByRole('main')).toHaveLength(0)
  })

  it('has an Export pill and no 100vh inline sizing', () => {
    const { container } = mount()
    expect(screen.getByRole('button', { name: /Export/ })).toHaveAttribute('aria-haspopup', 'menu')
    expect(container.innerHTML).not.toMatch(/100vh/)
  })
})

describe('Edit with AI', () => {
  it('opens the chat drawer for a draft when the role can generate', () => {
    mount()
    const btn = screen.getByRole('button', { name: 'Edit with AI' })
    expect(btn).toBeEnabled()
    fireEvent.click(btn)
    expect(screen.getByRole('dialog', { name: 'Edit with AI' })).toBeInTheDocument()
    expect(chat.lastProps?.protocolId).toBe('p1')

    fireEvent.click(screen.getByRole('button', { name: 'Close drawer' }))
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('is disabled, with the reason, for an approved (signed and locked) protocol', () => {
    mount({ status: 'approved' })
    const btn = screen.getByRole('button', { name: 'Edit with AI' })
    expect(btn).toBeDisabled()
    expect(btn).toHaveAttribute('aria-disabled', 'true')
    const reason = /signed and locked/
    expect(btn.parentElement).toHaveAttribute('title', expect.stringMatching(reason))
    const describedBy = btn.getAttribute('aria-describedby')
    expect(describedBy).toBeTruthy()
    expect(document.getElementById(describedBy!)).toHaveTextContent(reason)

    fireEvent.click(btn)
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('is disabled for a protocol that is in review', () => {
    mount({ status: 'review' })
    expect(screen.getByRole('button', { name: 'Edit with AI' })).toBeDisabled()
  })

  it('is disabled when the acting role cannot generate', () => {
    caps.value = { role: 'reviewer', is_org_owner: false, is_admin: false, can_generate: false }
    mount()
    const btn = screen.getByRole('button', { name: 'Edit with AI' })
    expect(btn).toBeDisabled()
    expect(btn.parentElement).toHaveAttribute('title', expect.stringMatching(/role cannot edit/))
  })

  it('reloads the protocol when an edit is applied, and says so if the reload fails', async () => {
    const onReload = vi.fn().mockRejectedValueOnce(new Error('network down'))
    mount({}, onReload)
    fireEvent.click(screen.getByRole('button', { name: 'Edit with AI' }))
    chat.lastProps?.onApplied?.()
    expect(onReload).toHaveBeenCalledTimes(1)
    expect(await screen.findByText(/network down/)).toBeInTheDocument()
  })
})
