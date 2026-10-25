import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
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
import BudgetsPage from '../app/BudgetsPage'

const ROW: BudgetSummary = {
  id: 'b1',
  name: 'Metformin BE budget',
  protocol_id: null,
  drug_inn: 'metformin',
  study_type: 'BE',
  status: 'draft',
  currency: 'USD',
  n_enrolled: 24,
  category_count: 17,
  line_item_count: 60,
  grand_total: 1000,
  created_at: '2026-10-01T10:00:00Z',
  updated_at: '2026-10-02T10:00:00Z',
  subject_profile: 'healthy',
  therapeutic_area: null,
  n_sites: null,
  n_countries: null,
}

function Where() {
  const loc = useLocation()
  return <p data-testid="where">{loc.pathname + loc.search}</p>
}

function mount() {
  return render(
    <MemoryRouter initialEntries={['/app/budgets']}>
      <main>
        <Routes>
          <Route path="/app/budgets" element={<BudgetsPage />} />
          <Route path="*" element={<Where />} />
        </Routes>
      </main>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  vi.mocked(api.listBudgets).mockResolvedValue([ROW])
})

describe('Budgets page', () => {
  it('has exactly one <main> landmark and a "New budget" pill that starts the budget chat', async () => {
    const { container } = mount()
    await screen.findByText('Metformin BE budget')
    expect(screen.getAllByRole('main')).toHaveLength(1)
    expect(container.querySelectorAll('main')).toHaveLength(1)

    fireEvent.click(screen.getByRole('button', { name: 'New budget' }))
    await waitFor(() => expect(screen.getByTestId('where')).toHaveTextContent('/app/chat/new?kind=budget'))
  })

  it('opening a budget goes to its URL', async () => {
    mount()
    await screen.findByText('Metformin BE budget')
    fireEvent.click(screen.getByRole('button', { name: 'Open Metformin BE budget' }))
    await waitFor(() => expect(screen.getByTestId('where')).toHaveTextContent('/app/budgets/b1'))
  })
})
