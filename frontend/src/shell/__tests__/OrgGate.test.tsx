import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'

vi.mock('../../lib/auth', () => ({
  authEnabled: false,
  AccountButton: () => null,
}))
vi.mock('../../lib/org', () => ({ useOrg: vi.fn() }))
vi.mock('../../api/chat', () => ({ createChat: vi.fn() }))
vi.mock('../../api/client', () => ({
  api: { createOrg: vi.fn(), joinOrg: vi.fn() },
}))

import { createChat } from '../../api/chat'
import { useOrg } from '../../lib/org'
import { PENDING_PROMPT_KEY, savePendingPrompt } from '../../lib/pendingPrompt'
import OrgGate from '../../components/OrgGate'
import Onboarding from '../../components/Onboarding'

function orgState(status: 'active' | 'pending' | 'suspended') {
  vi.mocked(useOrg).mockReturnValue({
    org: {
      id: 'o1',
      name: 'Acme',
      slug: 'acme',
      is_owner: true,
      join_token: null,
      license: { status, seat_count: 3, seats_used: 1, period_start: '', period_end: '' },
    },
    loading: false,
    needsOnboarding: false,
    orgLoadError: null,
    refresh: async () => {},
  })
}

const TEXT = 'Biosimilar PK study of adalimumab in healthy volunteers'

beforeEach(() => {
  window.localStorage.clear()
  vi.mocked(createChat).mockReset()
})

describe('OrgGate pending-license screen', () => {
  it('shows the saved study description and keeps the browser copy when saving to the server fails', async () => {
    orgState('pending')
    savePendingPrompt({ text: TEXT, kind: 'protocol' })
    vi.mocked(createChat).mockRejectedValue(new Error('404'))

    render(<OrgGate>app</OrgGate>)

    expect(screen.getByRole('heading', { name: 'Your study description is saved' })).toBeInTheDocument()
    expect(screen.getByText(TEXT)).toBeInTheDocument()
    await waitFor(() => expect(createChat).toHaveBeenCalledTimes(1))
    expect(createChat).toHaveBeenCalledWith({ kind: 'protocol', initial_message: TEXT })
    // Failure is silent and the text is still stored for the next attempt.
    expect(window.localStorage.getItem(PENDING_PROMPT_KEY)).not.toBeNull()
    expect(screen.getByText(/saved in this browser/i)).toBeInTheDocument()
    expect(screen.queryByText('app')).not.toBeInTheDocument()
  })

  it('moves the text to the workspace and clears the browser copy once the server accepts it', async () => {
    orgState('pending')
    savePendingPrompt({ text: TEXT, kind: 'budget' })
    vi.mocked(createChat).mockResolvedValue({} as never)

    render(<OrgGate>app</OrgGate>)

    await waitFor(() => expect(window.localStorage.getItem(PENDING_PROMPT_KEY)).toBeNull())
    expect(createChat).toHaveBeenCalledWith({ kind: 'budget', initial_message: TEXT })
    expect(await screen.findByText(/saved in your workspace/i)).toBeInTheDocument()
    // The description stays visible after the browser copy is gone.
    expect(screen.getByText(TEXT)).toBeInTheDocument()
  })

  it('shows no saved-description block and makes no request when nothing was typed', () => {
    orgState('pending')
    render(<OrgGate>app</OrgGate>)
    expect(screen.getByRole('heading', { name: /setting up your workspace/i })).toBeInTheDocument()
    expect(screen.queryByText('Your study description is saved')).not.toBeInTheDocument()
    expect(createChat).not.toHaveBeenCalled()
  })

  it('does not save anything for a suspended license', () => {
    orgState('suspended')
    savePendingPrompt({ text: TEXT, kind: 'protocol' })
    render(<OrgGate>app</OrgGate>)
    expect(screen.getByRole('heading', { name: /currently suspended/i })).toBeInTheDocument()
    expect(createChat).not.toHaveBeenCalled()
    expect(window.localStorage.getItem(PENDING_PROMPT_KEY)).not.toBeNull()
  })

  it('renders the app for an active license', () => {
    orgState('active')
    render(<OrgGate>app</OrgGate>)
    expect(screen.getByText('app')).toBeInTheDocument()
  })
})

describe('Onboarding', () => {
  it('toggles between creating and joining, and gates the submit button on input', () => {
    render(<Onboarding />)
    const create = screen.getByRole('button', { name: 'Create organization', pressed: true })
    expect(create).toBeInTheDocument()
    const submit = () => screen.getAllByRole('button', { name: 'Create organization' }).find(b => b.getAttribute('type') === 'submit')!
    expect(submit()).toBeDisabled()

    // Dev mode (auth off) needs an email as well as a name.
    fireEvent.change(screen.getByLabelText('Organization name'), { target: { value: 'Acme Pharma' } })
    expect(submit()).toBeDisabled()
    fireEvent.change(screen.getByLabelText('Your email'), { target: { value: 'ada@acme.test' } })
    expect(submit()).toBeEnabled()

    fireEvent.click(screen.getByRole('button', { name: 'Join a team' }))
    expect(screen.getByLabelText('Invite link or code')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Join' })).toBeDisabled()
  })
})
