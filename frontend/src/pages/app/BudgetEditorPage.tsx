import { useEffect, useState } from 'react'
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom'
import { Banner, ButtonLink, ChevronLeft, Spinner } from '../../ui'
import BudgetEditor from '../../components/BudgetEditor'
import { api } from '../../api/client'
import { crumbKey, useSetCrumb } from '../../lib/breadcrumbs'
import { useActingUser } from '../../lib/actingUser'
import type { Budget } from '../../types'

/**
 * `/app/budgets/:id`: loads a budget and opens the editor. A budget that was
 * just created arrives with router state `{ justCreated: true }` so the editor
 * shows its one-time "ready" banner; the flag is cleared from history after
 * the editor has read it, so a reload does not show it again.
 */
export default function BudgetEditorPage() {
  const { id = '' } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const location = useLocation()
  const { currentUser } = useActingUser()
  const [budget, setBudget] = useState<Budget | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [justCreated] = useState(() => (location.state as { justCreated?: boolean } | null)?.justCreated === true)

  useEffect(() => {
    let cancelled = false
    setBudget(null)
    setError(null)
    api
      .getBudget(id)
      .then(b => {
        if (!cancelled) setBudget(b)
      })
      .catch(e => {
        if (!cancelled) setError(e instanceof Error ? e.message : 'Could not open this budget.')
      })
    return () => {
      cancelled = true
    }
  }, [id, currentUser?.id])

  // Consume the one-time flag.
  useEffect(() => {
    if (justCreated && budget) navigate(location.pathname, { replace: true, state: null })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [justCreated, budget?.id])

  useSetCrumb(crumbKey.budget(id), budget?.name)

  if (error) {
    return (
      <div className="mx-auto max-w-2xl px-4 py-10">
        <Banner tone="danger" title="Could not open this budget">
          {error}
        </Banner>
        <div className="mt-4">
          <ButtonLink as={Link} to="/app/budgets" variant="secondary" leftIcon={<ChevronLeft />}>
            Back to budgets
          </ButtonLink>
        </div>
      </div>
    )
  }

  if (!budget) {
    return (
      <div className="flex min-h-[40vh] items-center justify-center gap-2 text-body text-fg-muted" role="status">
        <Spinner size={18} />
        <span>Opening budget…</span>
      </div>
    )
  }

  return (
    <BudgetEditor
      key={budget.id}
      budget={budget}
      onBack={() => navigate('/app/budgets')}
      onChanged={setBudget}
      justCreated={justCreated}
    />
  )
}
