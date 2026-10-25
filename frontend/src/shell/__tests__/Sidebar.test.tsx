import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { SIDEBAR_COLLAPSED_KEY, readSidebarCollapsed } from '../useSidebarCollapsed'
import { isMainNavActive, isPinnedActive, PINNED } from '../nav'

// Auth off: no Clerk anywhere in these tests.
vi.mock('../../lib/auth', () => ({
  authEnabled: false,
  AccountButton: () => null,
}))

// The chat API does not exist yet; the shell must cope with that quietly.
vi.mock('../chatList', () => ({
  fetchRecentChats: vi.fn(),
  CHAT_KIND_LABELS: { protocol: 'Protocol', budget: 'Budget', protocol_edit: 'Protocol edit' },
}))

// Written without importOriginal on purpose: api/client and lib/actingUser import each
// other, and loading the real client inside the factory hands actingUser the REAL api.
vi.mock('../../api/client', () => {
  class HttpError extends Error {
    status: number
    constructor(status: number, message: string) {
      super(message)
      this.status = status
    }
  }
  return {
    HttpError,
    API_BASE: '',
    authHeaders: vi.fn(async () => ({})),
    getCapabilities: vi.fn(),
    api: {
      listUsers: vi.fn(),
      getCurrentUser: vi.fn(),
      getMyOrg: vi.fn(),
      listProtocols: vi.fn(),
    },
  }
})

import { api, getCapabilities } from '../../api/client'
import { ActingUserProvider } from '../../lib/actingUser'
import { OrgProvider } from '../../lib/org'
import { fetchRecentChats } from '../chatList'
import AppShell from '../AppShell'

const ORG = {
  id: 'o1',
  name: 'Acme Pharma',
  slug: 'acme',
  is_owner: true,
  join_token: 'tok',
  license: { status: 'active', seat_count: 5, seats_used: 2, period_start: '', period_end: '' },
}

function stubMatchMedia(matches: (query: string) => boolean) {
  window.matchMedia = ((query: string) => ({
    matches: matches(query),
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia
}

function renderShell(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route
          path="/app"
          element={
            <ActingUserProvider>
              <OrgProvider>
                <AppShell />
              </OrgProvider>
            </ActingUserProvider>
          }
        >
          <Route index element={<p>Home page</p>} />
          <Route path="protocols" element={<p>Protocols page</p>} />
          <Route path="team" element={<p>Team page</p>} />
        </Route>
      </Routes>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  window.localStorage.clear()
  stubMatchMedia(() => true) // desktop
  vi.mocked(api.listUsers).mockResolvedValue([{ id: 'u1', org_id: 'o1', email: 'ada@acme.test', role: 'generator' }])
  vi.mocked(api.getMyOrg).mockResolvedValue(ORG as never)
  vi.mocked(api.listProtocols).mockResolvedValue([])
  vi.mocked(getCapabilities).mockResolvedValue({ role: 'generator', is_org_owner: true, is_admin: false, can_generate: true })
  vi.mocked(fetchRecentChats).mockResolvedValue({ items: [], available: false })
})

describe('nav active rules', () => {
  it('matches Home only on the app index', () => {
    expect(isMainNavActive('home', '/app')).toBe(true)
    expect(isMainNavActive('home', '/app/protocols')).toBe(false)
  })

  it('keeps list sections active on their detail pages, and /app/protocol (wizard) off Protocols', () => {
    expect(isMainNavActive('protocols', '/app/protocols/abc')).toBe(true)
    expect(isMainNavActive('protocols', '/app/protocol')).toBe(false)
    expect(isMainNavActive('budgets', '/app/budgets/abc')).toBe(true)
    expect(isMainNavActive('budgets', '/app/budgeting')).toBe(false)
  })

  it('New study covers the chat routes and a wizard opened without a preset', () => {
    expect(isMainNavActive('new', '/app/chat/new')).toBe(true)
    expect(isMainNavActive('new', '/app/chat/abc')).toBe(true)
    expect(isMainNavActive('new', '/app/protocol')).toBe(true)
    expect(isMainNavActive('new', '/app/protocol', '?preset=be')).toBe(false)
  })

  it('a pinned item is active only for its own preset', () => {
    const be = PINNED.find(p => p.preset === 'be')!
    const food = PINNED.find(p => p.preset === 'food_effect')!
    // CHAT_ENABLED is on: pinned items open the chat page for their kind + preset.
    expect(isPinnedActive(be, '/app/chat/new', '?kind=protocol&preset=be')).toBe(true)
    expect(isPinnedActive(food, '/app/chat/new', '?kind=protocol&preset=be')).toBe(false)
    expect(isPinnedActive(be, '/app/chat/new', '?kind=protocol')).toBe(false)
  })
})

describe('useSidebarCollapsed storage', () => {
  it('reads false by default and "1" as collapsed', () => {
    expect(readSidebarCollapsed()).toBe(false)
    window.localStorage.setItem(SIDEBAR_COLLAPSED_KEY, '1')
    expect(readSidebarCollapsed()).toBe(true)
  })

  it('does not throw when storage is blocked', () => {
    const spy = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    expect(readSidebarCollapsed()).toBe(false)
    spy.mockRestore()
  })
})

describe('AppShell sidebar', () => {
  it('marks only the current page with aria-current="page"', async () => {
    renderShell('/app/protocols')
    const main = await screen.findByRole('navigation', { name: 'Main' })
    const links = within(main).getAllByRole('link')
    const current = links.filter(l => l.getAttribute('aria-current') === 'page')
    expect(current).toHaveLength(1)
    expect(current[0]).toHaveTextContent('Protocols')
    expect(within(main).getByRole('link', { name: 'Home' })).not.toHaveAttribute('aria-current')
    expect(screen.getByText('Protocols page')).toBeInTheDocument()
  })

  it('shows the workspace card with the org name, license and seats', async () => {
    renderShell('/app')
    const trigger = await screen.findByRole('button', { name: /Workspace menu, Acme Pharma/ })
    expect(trigger).toHaveTextContent('Acme Pharma')
    expect(trigger).toHaveTextContent('Active')
    expect(trigger).toHaveTextContent('2/5 seats')
  })

  it('collapses to the icon rail, remembers it, and restores it on the next visit', async () => {
    const first = renderShell('/app')
    const collapse = await screen.findByRole('button', { name: 'Collapse sidebar' })
    expect(document.getElementById('app-sidebar')).toHaveAttribute('data-rail', 'false')

    fireEvent.click(collapse)
    expect(document.getElementById('app-sidebar')).toHaveAttribute('data-rail', 'true')
    expect(window.localStorage.getItem(SIDEBAR_COLLAPSED_KEY)).toBe('1')
    expect(screen.getByRole('button', { name: 'Expand sidebar' })).toBeInTheDocument()
    // Labels stay available to assistive tech in the rail.
    expect(within(screen.getByRole('navigation', { name: 'Main' })).getByRole('link', { name: 'Protocols' })).toBeInTheDocument()

    first.unmount()
    renderShell('/app')
    await screen.findByRole('button', { name: 'Expand sidebar' })
    expect(document.getElementById('app-sidebar')).toHaveAttribute('data-rail', 'true')

    fireEvent.click(screen.getByRole('button', { name: 'Expand sidebar' }))
    expect(window.localStorage.getItem(SIDEBAR_COLLAPSED_KEY)).toBe('0')
  })

  it('does not show the Admin link to non-admins, and shows it to admins', async () => {
    const first = renderShell('/app')
    await screen.findByRole('navigation', { name: 'Main' })
    await waitFor(() => expect(getCapabilities).toHaveBeenCalled())
    expect(screen.queryByRole('link', { name: 'Admin console' })).not.toBeInTheDocument()
    first.unmount()

    vi.mocked(getCapabilities).mockResolvedValue({ role: 'generator', is_org_owner: true, is_admin: true, can_generate: true })
    renderShell('/app')
    expect(await screen.findByRole('link', { name: 'Admin console' })).toHaveAttribute('href', '/admin')
  })

  it('hides the Recent section when the chat API is unavailable', async () => {
    renderShell('/app')
    await screen.findByRole('navigation', { name: 'Main' })
    await waitFor(() => expect(fetchRecentChats).toHaveBeenCalled())
    expect(screen.queryByRole('navigation', { name: 'Recent conversations' })).not.toBeInTheDocument()
  })

  it('lists recent chats when the chat API answers', async () => {
    vi.mocked(fetchRecentChats).mockResolvedValue({
      available: true,
      items: [
        { id: 's1', kind: 'protocol', status: 'active', title: 'Adalimumab biosimilar', protocol_id: null, budget_id: null, created_at: '2026-10-01T10:00:00Z', last_activity_at: '2026-10-01T10:00:00Z' },
      ],
    })
    renderShell('/app')
    const recent = await screen.findByRole('navigation', { name: 'Recent conversations' })
    expect(within(recent).getByRole('link', { name: /Adalimumab biosimilar/ })).toHaveAttribute('href', '/app/chat/s1')
  })

  it('lights the generation dot only while a protocol is generating', async () => {
    vi.mocked(api.listProtocols).mockResolvedValue([
      { id: 'p1', drug_id: 'd1', drug_inn: 'adalimumab', drug_brand_names: [], study_type: 'biosimilar_pk', guideline_version: 'FDA-BE-2014', guideline_outdated: false, version: 1, status: 'draft', section_count: 0, created_at: '2026-10-01T10:00:00Z', generation_status: 'generating' } as never,
    ])
    renderShell('/app')
    expect(await screen.findByRole('link', { name: /being generated/ })).toBeInTheDocument()
  })

  it('shows no generation dot when nothing is generating', async () => {
    renderShell('/app')
    await screen.findByRole('navigation', { name: 'Main' })
    await waitFor(() => expect(api.listProtocols).toHaveBeenCalled())
    expect(screen.queryByRole('link', { name: /being generated/ })).not.toBeInTheDocument()
  })
})

describe('AppShell on a phone', () => {
  it('opens the drawer from the hamburger and closes it with Esc, returning focus', async () => {
    stubMatchMedia(() => false) // below md and lg
    renderShell('/app')
    const burger = await screen.findByRole('button', { name: 'Open navigation' })
    expect(document.getElementById('app-sidebar')).toBeNull()

    fireEvent.click(burger)
    const dialog = await screen.findByRole('dialog', { name: 'Navigation' })
    expect(dialog).toHaveAttribute('aria-modal', 'true')
    expect(within(dialog).getByRole('link', { name: 'Protocols' })).toBeInTheDocument()
    expect(dialog.contains(document.activeElement)).toBe(true)

    fireEvent.keyDown(document.activeElement as HTMLElement, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Navigation' })).not.toBeInTheDocument())
    expect(burger).toHaveFocus()
  })

  it('keeps Tab inside the open drawer (wraps from the last control to the first and back)', async () => {
    stubMatchMedia(() => false)
    renderShell('/app')
    fireEvent.click(await screen.findByRole('button', { name: 'Open navigation' }))
    const dialog = await screen.findByRole('dialog', { name: 'Navigation' })
    const focusable = Array.from(dialog.querySelectorAll<HTMLElement>('a[href], button:not([disabled])'))
    expect(focusable.length).toBeGreaterThan(3)
    const first = focusable[0]
    const last = focusable[focusable.length - 1]

    last.focus()
    fireEvent.keyDown(last, { key: 'Tab' })
    expect(first).toHaveFocus()

    fireEvent.keyDown(first, { key: 'Tab', shiftKey: true })
    expect(last).toHaveFocus()
  })

  it('closes the drawer when a link is followed', async () => {
    stubMatchMedia(() => false)
    renderShell('/app')
    fireEvent.click(await screen.findByRole('button', { name: 'Open navigation' }))
    const dialog = await screen.findByRole('dialog', { name: 'Navigation' })
    fireEvent.click(within(dialog).getByRole('link', { name: 'Team' }))
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Navigation' })).not.toBeInTheDocument())
    expect(screen.getByText('Team page')).toBeInTheDocument()
  })
})
