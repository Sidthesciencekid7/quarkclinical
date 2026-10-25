import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { ProtocolSummary } from '../../../types'

// Written without importOriginal on purpose: api/client and lib/actingUser import each other.
vi.mock('../../../api/client', () => ({
  api: { listProtocols: vi.fn(), cloneProtocol: vi.fn() },
}))
vi.mock('../../../lib/actingUser', () => ({
  useActingUser: () => ({ currentUser: { id: 'u1', role: 'generator' }, loading: false, error: null }),
}))

import { api } from '../../../api/client'
import ProtocolsPage from '../ProtocolsPage'

function row(over: Partial<ProtocolSummary> & Pick<ProtocolSummary, 'id' | 'drug_inn'>): ProtocolSummary {
  return {
    drug_id: `d-${over.id}`,
    drug_brand_names: [],
    study_type: 'BE',
    guideline_version: 'FDA-BE-2014',
    guideline_outdated: false,
    version: 1,
    status: 'draft',
    section_count: 17,
    created_at: '2026-10-01T10:00:00Z',
    ...over,
  }
}

const ROWS: ProtocolSummary[] = [
  row({ id: 'a', drug_inn: 'metformin', status: 'draft' }),
  row({ id: 'b', drug_inn: 'adalimumab', study_type: 'biosimilar_pk', status: 'review', drug_brand_names: ['Humira'] }),
  row({ id: 'c', drug_inn: 'ibuprofen', status: 'approved' }),
  row({ id: 'd', drug_inn: 'ketoprofen', status: 'draft' }),
]

function Where() {
  const loc = useLocation()
  return <p data-testid="where">{loc.pathname + loc.search}</p>
}

/** Mimics the shell: ONE <main> around the routed page. */
function mount() {
  return render(
    <MemoryRouter initialEntries={['/app/protocols']}>
      <main>
        <Routes>
          <Route path="/app/protocols" element={<ProtocolsPage />} />
          <Route path="*" element={<Where />} />
        </Routes>
      </main>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  vi.mocked(api.listProtocols).mockResolvedValue(ROWS)
})

describe('Protocols page (ReportsView)', () => {
  it('renders exactly one <main> landmark, a page heading and the New study pill', async () => {
    const { container } = mount()
    await screen.findByText('Metformin')
    expect(screen.getAllByRole('main')).toHaveLength(1)
    expect(container.querySelectorAll('main')).toHaveLength(1)
    expect(screen.getByRole('heading', { level: 1, name: 'Protocols' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'New study' })).toBeInTheDocument()
  })

  it('"New study" goes to the protocol chat', async () => {
    render(
      <MemoryRouter initialEntries={['/app/protocols']}>
        <Routes>
          <Route path="/app/protocols" element={<ProtocolsPage />} />
          <Route path="*" element={<Where />} />
        </Routes>
      </MemoryRouter>,
    )
    fireEvent.click(await screen.findByRole('button', { name: 'New study' }))
    expect(screen.getByTestId('where')).toHaveTextContent('/app/chat/new?kind=protocol')
  })

  it('status tabs filter the list: All, Draft, In review, Approved', async () => {
    mount()
    await screen.findByText('Metformin')
    const tabs = screen.getByRole('tablist', { name: 'Filter protocols by status' })
    const list = () => screen.getByRole('tabpanel')
    const names = () => within(list()).getAllByRole('heading', { level: 2 }).map(h => h.textContent)

    expect(within(tabs).getByRole('tab', { name: /^All/ })).toHaveAttribute('aria-selected', 'true')
    expect(names()).toEqual(['Metformin', 'Adalimumab', 'Ibuprofen', 'Ketoprofen'])

    fireEvent.click(within(tabs).getByRole('tab', { name: /^Draft/ }))
    expect(within(tabs).getByRole('tab', { name: /^Draft/ })).toHaveAttribute('aria-selected', 'true')
    expect(names()).toEqual(['Metformin', 'Ketoprofen'])

    fireEvent.click(within(tabs).getByRole('tab', { name: /^In review/ }))
    expect(names()).toEqual(['Adalimumab'])

    fireEvent.click(within(tabs).getByRole('tab', { name: /^Approved/ }))
    expect(names()).toEqual(['Ibuprofen'])

    fireEvent.click(within(tabs).getByRole('tab', { name: /^All/ }))
    expect(names()).toHaveLength(4)
  })

  it('shows a count on each tab', async () => {
    mount()
    await screen.findByText('Metformin')
    const tabs = screen.getByRole('tablist', { name: 'Filter protocols by status' })
    expect(within(tabs).getByRole('tab', { name: /^All\s*4$/ })).toBeInTheDocument()
    expect(within(tabs).getByRole('tab', { name: /^Draft\s*2$/ })).toBeInTheDocument()
    expect(within(tabs).getByRole('tab', { name: /^In review\s*1$/ })).toBeInTheDocument()
    expect(within(tabs).getByRole('tab', { name: /^Approved\s*1$/ })).toBeInTheDocument()
  })

  it('search filters by drug, brand name and study type, and combines with the tab', async () => {
    mount()
    await screen.findByText('Metformin')
    const search = screen.getByRole('searchbox', { name: /Search protocols/ })
    const names = () => within(screen.getByRole('tabpanel')).getAllByRole('heading', { level: 2 }).map(h => h.textContent)

    fireEvent.change(search, { target: { value: 'adal' } })
    expect(names()).toEqual(['Adalimumab'])

    fireEvent.change(search, { target: { value: 'humira' } })
    expect(names()).toEqual(['Adalimumab'])

    fireEvent.change(search, { target: { value: 'profen' } })
    expect(names()).toEqual(['Ibuprofen', 'Ketoprofen'])

    fireEvent.click(screen.getByRole('tab', { name: /^Approved/ }))
    expect(names()).toEqual(['Ibuprofen'])

    fireEvent.change(search, { target: { value: 'zzz' } })
    expect(screen.getByText('No protocols match')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Clear filters' }))
    expect(names()).toHaveLength(4)
    expect(search).toHaveValue('')
  })

  it('the Open pill navigates to the protocol editor', async () => {
    render(
      <MemoryRouter initialEntries={['/app/protocols']}>
        <Routes>
          <Route path="/app/protocols" element={<ProtocolsPage />} />
          <Route path="*" element={<Where />} />
        </Routes>
      </MemoryRouter>,
    )
    fireEvent.click(await screen.findByRole('button', { name: 'Open Adalimumab protocol' }))
    expect(screen.getByTestId('where')).toHaveTextContent('/app/protocols/b')
  })

  it('duplicates a protocol and refreshes the list', async () => {
    vi.mocked(api.cloneProtocol).mockResolvedValue({} as never)
    mount()
    fireEvent.click(await screen.findByRole('button', { name: 'Duplicate Metformin protocol' }))
    await waitFor(() => expect(api.cloneProtocol).toHaveBeenCalledWith('a'))
    await waitFor(() => expect(api.listProtocols).toHaveBeenCalledTimes(2))
  })

  it('shows an empty state with a New study action when there are no protocols', async () => {
    vi.mocked(api.listProtocols).mockResolvedValue([])
    mount()
    expect(await screen.findByText('No protocols yet')).toBeInTheDocument()
    expect(screen.queryByRole('tablist')).toBeNull()
  })
})
