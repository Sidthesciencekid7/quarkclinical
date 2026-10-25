import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { AdminOrgRow, Lead } from '../../types'

// The page picks its Clerk-aware or Clerk-free variant from this module constant
// at render time; the getter lets one test file exercise both. There is NO
// ClerkProvider anywhere in this file, exactly like a local sign-in-off run.
const mocks = vi.hoisted(() => ({
  authEnabled: false,
  signOut: vi.fn(),
  useClerk: vi.fn(),
}))

vi.mock('../../lib/auth', () => ({
  get authEnabled() {
    return mocks.authEnabled
  },
}))
vi.mock('@clerk/clerk-react', () => ({
  useClerk: () => {
    mocks.useClerk()
    return { signOut: mocks.signOut }
  },
}))
// Written without importOriginal on purpose: api/client and lib/actingUser import each other.
vi.mock('../../api/client', () => ({
  api: {
    listLeads: vi.fn(),
    adminListOrgs: vi.fn(),
    activateLicense: vi.fn(),
    suspendLicense: vi.fn(),
    deleteLead: vi.fn(),
  },
}))

import { api } from '../../api/client'
import { AdminFrame } from '../../shell/AdminFrame'
import AdminDashboard from '../AdminDashboard'

const LEADS = [
  {
    id: 'l1',
    company_name: 'Acme Bio',
    contact_name: 'Ada Lovelace',
    contact_email: 'ada@acme.test',
    team_size_estimate: '11-50',
    message: 'We would like a demo',
    status: 'new',
    created_at: '2026-10-01T10:00:00Z',
  },
] as unknown as Lead[]

const ORGS = [
  {
    id: 'o1',
    name: 'Acme Bio',
    slug: 'acme-bio',
    owner_email: 'owner@acme.test',
    license_status: 'pending',
    days_remaining: null,
    seats_used: 1,
    seat_count: 5,
    created_at: '2026-10-01T10:00:00Z',
  },
  {
    id: 'o2',
    name: 'Globex',
    slug: 'globex',
    owner_email: 'owner@globex.test',
    license_status: 'active',
    days_remaining: 30,
    seats_used: 2,
    seat_count: 10,
    created_at: '2026-09-01T10:00:00Z',
  },
] as unknown as AdminOrgRow[]

function mount() {
  return render(
    <MemoryRouter>
      <AdminDashboard />
    </MemoryRouter>,
  )
}

beforeEach(() => {
  mocks.authEnabled = false
  mocks.signOut.mockReset()
  mocks.useClerk.mockReset()
  vi.mocked(api.listLeads).mockResolvedValue(LEADS)
  vi.mocked(api.adminListOrgs).mockResolvedValue(ORGS)
})

describe('AdminDashboard with sign-in OFF (no ClerkProvider)', () => {
  it('renders the dashboard instead of crashing, without touching Clerk', async () => {
    mount()
    expect(await screen.findByRole('heading', { level: 1, name: 'Admin dashboard' })).toBeInTheDocument()
    expect(mocks.useClerk).not.toHaveBeenCalled()
    expect(screen.getByRole('heading', { level: 2, name: 'Contact sales leads (1)' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 2, name: 'Organizations (2)' })).toBeInTheDocument()
    expect(screen.getAllByText('Acme Bio')).toHaveLength(2) // lead company + org row
    // No Log out: there is nothing to sign out of. The page says why.
    expect(screen.queryByRole('button', { name: 'Log out' })).not.toBeInTheDocument()
    expect(screen.getByText(/Sign-in is off in this environment/)).toBeInTheDocument()
  })

  it('owns exactly one <main> landmark (it lives outside the app shell)', async () => {
    const { container } = mount()
    await screen.findByRole('heading', { name: 'Admin dashboard' })
    expect(screen.getAllByRole('main')).toHaveLength(1)
    expect(container.querySelectorAll('main')).toHaveLength(1)
  })

  it('inside AdminFrame it shows the "Back to app" pill next to the page', async () => {
    render(
      <MemoryRouter initialEntries={['/admin']}>
        <AdminFrame>
          <AdminDashboard />
        </AdminFrame>
      </MemoryRouter>,
    )
    await screen.findByRole('heading', { name: 'Admin dashboard' })
    const back = screen.getByRole('link', { name: 'Back to app' })
    expect(back).toHaveAttribute('href', '/app')
    expect(back).toHaveClass('btn', 'btn-secondary')
    expect(screen.getAllByRole('main')).toHaveLength(1)
  })

  it('license badges and per-row actions follow the license state', async () => {
    mount()
    await screen.findByRole('heading', { name: 'Admin dashboard' })
    const rows = screen.getAllByRole('row')
    const pending = rows.find(r => within(r).queryByText('acme-bio'))!
    expect(within(pending).getByText('pending')).toBeInTheDocument()
    expect(within(pending).getByRole('button', { name: 'Activate' })).toBeInTheDocument()
    const active = rows.find(r => within(r).queryByText('globex'))!
    expect(within(active).getByText('active')).toBeInTheDocument()
    expect(within(active).getByRole('button', { name: 'Suspend' })).toBeInTheDocument()
  })

  it('Activate calls the API and reloads', async () => {
    vi.mocked(api.activateLicense).mockResolvedValue(undefined as never)
    mount()
    await screen.findByRole('heading', { name: 'Admin dashboard' })
    fireEvent.click(screen.getByRole('button', { name: 'Activate' }))
    await waitFor(() => expect(api.activateLicense).toHaveBeenCalledWith('o1'))
    await waitFor(() => expect(api.listLeads).toHaveBeenCalledTimes(2))
  })

  it('a failed load shows "Access denied" with a retry, and no Log out', async () => {
    vi.mocked(api.listLeads).mockRejectedValueOnce(new Error('Admin access required'))
    mount()
    expect(await screen.findByRole('heading', { name: 'Access denied' })).toBeInTheDocument()
    expect(screen.getByText('Admin access required')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Log out' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    expect(await screen.findByRole('heading', { name: 'Admin dashboard' })).toBeInTheDocument()
  })
})

describe('AdminDashboard with sign-in ON', () => {
  it('shows Log out, which signs out and returns to /admin', async () => {
    mocks.authEnabled = true
    mount()
    await screen.findByRole('heading', { name: 'Admin dashboard' })
    expect(screen.queryByText(/Sign-in is off/)).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Log out' }))
    expect(mocks.signOut).toHaveBeenCalledWith({ redirectUrl: '/admin' })
  })
})
