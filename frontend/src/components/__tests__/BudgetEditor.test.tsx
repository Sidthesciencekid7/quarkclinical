import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { Budget, BudgetCategory, BudgetLineItem, BudgetRollup } from '../../types'

// Written without importOriginal on purpose: api/client and lib/actingUser import each other.
vi.mock('../../api/client', () => ({
  api: {
    updateBudget: vi.fn(),
    getBudgetRollup: vi.fn(),
    exportBudgetDocx: vi.fn(),
    updateBudgetLineItem: vi.fn(),
  },
  HttpError: class HttpError extends Error {
    status = 0
  },
}))

import { api } from '../../api/client'
import BudgetEditor from '../BudgetEditor'

const ROLLUP: BudgetRollup = {
  direct_subtotal: 12345,
  discount_type: 'none',
  discount_value: 0,
  discount_amount: 0,
  total_direct_costs: 12345,
  subject_stipend_total: 0,
  variable_total: 0,
  pass_through_total: 0,
  investigator_grants_total: 0,
  software_total: 0,
  grand_total: 12345,
  category_subtotals: { c1: 12345 },
}

const LINE: BudgetLineItem = {
  id: 'li1',
  category_id: 'c1',
  label: 'Screening visit',
  quantity: 10,
  unit_cost: 1234.5,
  unit_description: 'visit',
  total: 12345,
  status: 'costed',
  source: 'rate_card',
  derivation_key: null,
  note: null,
  sort_order: 0,
  created_at: '2026-10-01T10:00:00Z',
  updated_at: '2026-10-01T10:00:00Z',
}

const CATEGORY: BudgetCategory = {
  id: 'c1',
  budget_id: 'b1',
  name: 'Clinical conduct',
  bucket: 'direct',
  sort_order: 0,
  note: null,
  line_items: [LINE],
  subtotal: 12345,
}

function makeBudget(over: Partial<Budget> = {}): Budget {
  return {
    id: 'b1',
    org_id: 'o1',
    protocol_id: null,
    name: 'Metformin BE budget',
    sponsor_name: null,
    cro_name: null,
    currency: 'USD',
    status: 'draft',
    drug_inn: 'metformin',
    study_type: 'BE',
    n_enrolled: 24,
    screen_failure_rate_pct: null,
    n_screen_failures: null,
    n_alternates: null,
    n_screened: null,
    n_sites: null,
    n_dosing_groups: null,
    project_duration_months: null,
    discount_type: 'none',
    discount_value: 0,
    notes: null,
    derivation: null,
    categories: [CATEGORY],
    rollup: ROLLUP,
    created_at: '2026-10-01T10:00:00Z',
    updated_at: '2026-10-01T10:00:00Z',
    subject_profile: 'healthy',
    therapeutic_area: null,
    indication: null,
    n_countries: null,
    n_sites_selected: null,
    dropout_rate_pct: null,
    startup_months: null,
    recruitment_months: null,
    treatment_months: null,
    followup_months: null,
    closeout_months: null,
    n_expected_saes: null,
    n_expected_susars: null,
    n_monitoring_visits_onsite: null,
    n_monitoring_visits_remote: null,
    n_pharmacy_visits: null,
    n_visits_per_patient: null,
    n_protocol_amendments: null,
    geography_profile: null,
    geography_factor: null,
    archival_retention_years: null,
    bicr_required: null,
    comparator_sourced_by_sponsor: null,
    comparator_arm_fraction: null,
    n_completed: null,
    ...over,
  }
}

function mount(budget: Budget = makeBudget(), over: { justCreated?: boolean } = {}) {
  const onBack = vi.fn()
  const onChanged = vi.fn()
  // Mimics the shell: ONE <main> around the routed page.
  const utils = render(
    <main>
      <BudgetEditor budget={budget} onBack={onBack} onChanged={onChanged} {...over} />
    </main>,
  )
  return { ...utils, onBack, onChanged }
}

beforeEach(() => {
  vi.mocked(api.updateBudget).mockImplementation(async (_id, patch) => makeBudget(patch as Partial<Budget>))
  vi.mocked(api.getBudgetRollup).mockResolvedValue(ROLLUP)
})

describe('BudgetEditor', () => {
  it('renders exactly one <main> landmark (the shell supplies it) and the budget name', () => {
    const { container } = mount()
    expect(screen.getAllByRole('main')).toHaveLength(1)
    expect(container.querySelectorAll('main')).toHaveLength(1)
    expect(screen.getByRole('textbox', { name: 'Budget name' })).toHaveValue('Metformin BE budget')
  })

  it('keeps the approximate-estimate disclaimer prominent (a warning banner), server text first', () => {
    const { unmount } = mount()
    // Fallback copy when the budget carries no derivation.
    expect(screen.getByRole('alert')).toHaveTextContent('Every figure here is an estimate')
    unmount()

    mount(
      makeBudget({
        derivation: {
          disclaimer: 'Every dollar figure in this budget is an APPROXIMATE planning estimate.',
          protocol_linked: false,
          derived_at: '2026-10-01T10:00:00Z',
          warnings: [],
          lines: [],
          assumptions: {},
          sources: {},
        } as unknown as Budget['derivation'],
      }),
    )
    expect(screen.getByRole('alert')).toHaveTextContent('APPROXIMATE planning estimate')
  })

  it('shows the grand total in the ledger card and the sticky bar, both tabular-nums', () => {
    mount()
    const money = screen.getAllByText('$12,345.00')
    // ledger "Direct subtotal", "Total direct costs", "Grand total", sticky bar, category subtotal.
    expect(money.length).toBeGreaterThanOrEqual(2)
    for (const el of money) expect(el).toHaveClass('tabular-nums')
    expect(screen.getByText('Grand total', { selector: 'span.text-small' })).toBeInTheDocument() // sticky bar label
  })

  it('status is a pill toggle group; choosing Final PATCHes the budget', async () => {
    const { onChanged } = mount()
    const group = screen.getByRole('group', { name: 'Budget status' })
    expect(within(group).getByRole('button', { name: 'Draft' })).toHaveAttribute('aria-pressed', 'true')

    fireEvent.click(within(group).getByRole('button', { name: 'Final' }))
    await waitFor(() => expect(api.updateBudget).toHaveBeenCalledWith('b1', { status: 'final' }))
    await waitFor(() => expect(onChanged).toHaveBeenCalledWith(expect.objectContaining({ status: 'final' })))
    await waitFor(() =>
      expect(within(group).getByRole('button', { name: 'Final' })).toHaveAttribute('aria-pressed', 'true'),
    )
  })

  it('"All budgets" goes back', () => {
    const { onBack } = mount()
    fireEvent.click(screen.getByRole('button', { name: 'All budgets' }))
    expect(onBack).toHaveBeenCalledTimes(1)
  })

  it('offers the Word export as a pill button', () => {
    mount()
    const btn = screen.getByRole('button', { name: 'Download Word (.docx)' })
    expect(btn).toHaveClass('btn', 'btn-primary')
  })

  it('shows the one-time ready banner right after creation and lets it be dismissed', () => {
    mount(makeBudget(), { justCreated: true })
    expect(screen.getByText(/Your budget is ready/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Download Word document' })).toBeInTheDocument()
    fireEvent.click(screen.getAllByRole('button', { name: 'Dismiss' })[0])
    expect(screen.queryByText(/Your budget is ready/)).not.toBeInTheDocument()
  })

  it('expanding a category reveals labelled, tabular-nums line-item inputs', () => {
    mount()
    expect(screen.queryByRole('textbox', { name: 'Quantity for Screening visit' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Expand all' }))
    const qty = screen.getByRole('textbox', { name: 'Quantity for Screening visit' })
    expect(qty).toHaveValue('10')
    expect(qty).toHaveClass('tabular-nums')
    expect(screen.getByRole('textbox', { name: 'Unit cost for Screening visit' })).toHaveValue('1234.5')
    expect(screen.getByRole('button', { name: 'Collapse all' })).toBeInTheDocument()
  })

  it('editing a line item still saves quantity AND unit cost together (total omitted)', async () => {
    vi.mocked(api.updateBudgetLineItem).mockResolvedValue({ ...LINE, quantity: 12, total: 14814 })
    mount()
    fireEvent.click(screen.getByRole('button', { name: 'Expand all' }))
    const qty = screen.getByRole('textbox', { name: 'Quantity for Screening visit' })
    fireEvent.change(qty, { target: { value: '12' } })
    fireEvent.blur(qty)
    await waitFor(() =>
      expect(api.updateBudgetLineItem).toHaveBeenCalledWith('b1', 'li1', { quantity: 12, unit_cost: 1234.5 }),
    )
  })
})
