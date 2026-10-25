import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import type { ComponentProps } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { BudgetSummary } from '../../types'

// Written without importOriginal on purpose: api/client and lib/actingUser import each other.
vi.mock('../../api/client', () => ({
  api: { listBudgets: vi.fn(), deleteBudget: vi.fn(), exportBudgetDocx: vi.fn() },
  HttpError: class HttpError extends Error {
    status = 0
  },
}))
vi.mock('../../lib/actingUser', () => ({
  useActingUser: () => ({ currentUser: { id: 'u1', role: 'generator' }, loading: false, error: null }),
}))

import { api } from '../../api/client'
import BudgetList from '../BudgetList'

function summary(over: Partial<BudgetSummary> & Pick<BudgetSummary, 'id' | 'name' | 'status'>): BudgetSummary {
  return {
    protocol_id: null,
    drug_inn: 'metformin',
    study_type: 'BE',
    currency: 'USD',
    n_enrolled: 24,
    category_count: 17,
    line_item_count: 60,
    grand_total: 1234567.5,
    created_at: '2026-10-01T10:00:00Z',
    updated_at: '2026-10-02T10:00:00Z',
    subject_profile: 'healthy',
    therapeutic_area: null,
    n_sites: null,
    n_countries: null,
    ...over,
  }
}

const ROWS: BudgetSummary[] = [
  summary({ id: 'a', name: 'Metformin BE budget', status: 'draft' }),
  summary({ id: 'b', name: 'Adalimumab PK budget', status: 'final', study_type: 'biosimilar_pk', protocol_id: 'p1' }),
  summary({ id: 'c', name: 'Ibuprofen BE budget', status: 'draft' }),
]

function mount(over: Partial<ComponentProps<typeof BudgetList>> = {}) {
  const props = {
    onOpen: vi.fn(),
    onNew: vi.fn(),
    openingId: null,
    openError: null,
    onDismissOpenError: vi.fn(),
    ...over,
  }
  // Mimics the shell: ONE <main> around the routed page.
  const utils = render(
    <main>
      <BudgetList {...props} />
    </main>,
  )
  return { ...utils, props }
}

function visibleNames(): string[] {
  return screen.queryAllByRole('heading', { level: 2 }).map(h => h.textContent ?? '')
}

beforeEach(() => {
  vi.mocked(api.listBudgets).mockResolvedValue(ROWS)
  vi.mocked(api.deleteBudget).mockResolvedValue(undefined as never)
})

describe('BudgetList', () => {
  it('shows a page heading, a "New budget" pill and one card per budget', async () => {
    const { props } = mount()
    await screen.findByText('Metformin BE budget')
    expect(screen.getByRole('heading', { level: 1, name: 'Budgets' })).toBeInTheDocument()
    expect(visibleNames()).toEqual(['Metformin BE budget', 'Adalimumab PK budget', 'Ibuprofen BE budget'])

    fireEvent.click(screen.getByRole('button', { name: 'New budget' }))
    expect(props.onNew).toHaveBeenCalledTimes(1)
  })

  it('renders money in tabular-nums', async () => {
    mount()
    const total = await screen.findAllByText('$1,234,567.50')
    expect(total).toHaveLength(3)
    for (const el of total) expect(el).toHaveClass('tabular-nums')
  })

  it('status tabs filter the list (All / Draft / Final / Archived) with counts', async () => {
    mount()
    await screen.findByText('Metformin BE budget')

    const tablist = screen.getByRole('tablist', { name: 'Filter budgets by status' })
    // The accessible name is "Draft 2" (label, then the count), so match on the label.
    const tab = (label: string) => within(tablist).getByRole('tab', { name: new RegExp(`^${label}`) })
    expect(within(tablist).getAllByRole('tab').map(t => t.textContent)).toEqual(['All3', 'Draft2', 'Final1', 'Archived0'])
    expect(tab('All')).toHaveAttribute('aria-selected', 'true')

    fireEvent.click(tab('Draft'))
    expect(tab('Draft')).toHaveAttribute('aria-selected', 'true')
    expect(visibleNames()).toEqual(['Metformin BE budget', 'Ibuprofen BE budget'])

    fireEvent.click(tab('Final'))
    expect(visibleNames()).toEqual(['Adalimumab PK budget'])

    fireEvent.click(tab('Archived'))
    expect(visibleNames()).toEqual([])
    expect(screen.getByText('No budgets match this filter')).toBeInTheDocument()

    // The empty-filter state offers a way back.
    fireEvent.click(screen.getByRole('button', { name: 'Show all budgets' }))
    expect(tab('All')).toHaveAttribute('aria-selected', 'true')
    expect(visibleNames()).toHaveLength(3)
  })

  it('moves between tabs with the arrow keys', async () => {
    mount()
    await screen.findByText('Metformin BE budget')
    const tablist = screen.getByRole('tablist', { name: 'Filter budgets by status' })
    fireEvent.keyDown(tablist, { key: 'ArrowRight' })
    expect(within(tablist).getByRole('tab', { name: /^Draft/ })).toHaveAttribute('aria-selected', 'true')
    expect(visibleNames()).toEqual(['Metformin BE budget', 'Ibuprofen BE budget'])
  })

  it('"Open" opens that budget', async () => {
    const { props } = mount()
    await screen.findByText('Adalimumab PK budget')
    fireEvent.click(screen.getByRole('button', { name: 'Open Adalimumab PK budget' }))
    expect(props.onOpen).toHaveBeenCalledWith(expect.objectContaining({ id: 'b' }))
  })

  it('delete is a two-step confirm and only the second click calls the API', async () => {
    mount()
    await screen.findByText('Metformin BE budget')
    vi.mocked(api.listBudgets).mockResolvedValue(ROWS.filter(r => r.id !== 'a'))

    fireEvent.click(screen.getByRole('button', { name: 'Delete Metformin BE budget' }))
    expect(api.deleteBudget).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: 'Confirm delete of Metformin BE budget' }))
    await waitFor(() => expect(api.deleteBudget).toHaveBeenCalledWith('a'))
    await waitFor(() => expect(screen.queryByText('Metformin BE budget')).not.toBeInTheDocument())
  })

  it('shows the empty state with a create pill when there are no budgets', async () => {
    vi.mocked(api.listBudgets).mockResolvedValue([])
    const { props } = mount()
    await screen.findByText('No budgets yet')
    // No filter tabs until there is something to filter.
    expect(screen.queryByRole('tablist')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Create your first budget' }))
    expect(props.onNew).toHaveBeenCalledTimes(1)
  })

  it('surfaces a load failure as an alert', async () => {
    vi.mocked(api.listBudgets).mockRejectedValue(new Error('boom'))
    mount()
    expect(await screen.findByText('boom')).toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent("Couldn't load your budgets")
  })

  it('renders its own <main>-free page: the shell supplies the only <main> landmark', async () => {
    const { container } = mount()
    await screen.findByText('Metformin BE budget')
    expect(screen.getAllByRole('main')).toHaveLength(1)
    expect(container.querySelectorAll('main')).toHaveLength(1)
  })

  it('does not render a <main> of its own', async () => {
    const { container } = render(
      <BudgetList onOpen={vi.fn()} onNew={vi.fn()} openingId={null} openError={null} onDismissOpenError={vi.fn()} />,
    )
    await screen.findByText('Metformin BE budget')
    expect(container.querySelector('main')).toBeNull()
  })
})
