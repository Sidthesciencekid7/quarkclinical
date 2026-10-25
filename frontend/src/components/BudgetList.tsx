import { useEffect, useMemo, useState } from 'react'
import type { MouseEvent } from 'react'
import { api } from '../api/client'
import { useActingUser } from '../lib/actingUser'
import { formatMoney, relativeDate } from './budgetFormat'
import { downloadBudgetDocx } from './BudgetExportButton'
import type { BudgetStatus, BudgetSummary, StudyType } from '../types'
import {
  ArrowRight,
  Badge,
  Banner,
  Button,
  Card,
  Download,
  EmptyState,
  Plus,
  Receipt,
  Search,
  TabPanel,
  Tabs,
  Trash2,
} from '../ui'
import type { BadgeVariant } from '../ui'

interface Props {
  /** Open a budget in the editor (parent fetches the full object). */
  onOpen: (summary: BudgetSummary) => void
  /** Start a new budget (switch to the create view). */
  onNew: () => void
  /** The id currently being opened, so its row can show a spinner. */
  openingId: string | null
  /** Error message from the last failed attempt to open a budget, if any. */
  openError: string | null
  /** Dismiss the current open-budget error banner. */
  onDismissOpenError: () => void
}

const STUDY_LABELS: Record<StudyType, string> = {
  BE: 'Bioequivalence',
  biosimilar_pk: 'Biosimilar PK',
  food_effect: 'Food Effect',
}

// Subject profile, spelled out in the meta line: a 22-category patient budget
// and a 17-category healthy one look identical until this tells you which is
// which.
const PROFILE_LABEL: Record<'healthy' | 'patient', string> = {
  healthy: 'Healthy volunteers',
  patient: 'Patient study',
}

const STATUS_BADGE: Record<BudgetStatus, { label: string; variant: BadgeVariant }> = {
  draft: { label: 'Draft', variant: 'neutral' },
  final: { label: 'Final', variant: 'success' },
  archived: { label: 'Archived', variant: 'neutral' },
}

type StatusFilter = 'all' | BudgetStatus

const FILTERS: { id: StatusFilter; label: string }[] = [
  { id: 'all', label: 'All' },
  { id: 'draft', label: 'Draft' },
  { id: 'final', label: 'Final' },
  { id: 'archived', label: 'Archived' },
]

const TABS_ID = 'budget-status-tabs'

export default function BudgetList({ onOpen, onNew, openingId, openError, onDismissOpenError }: Props) {
  const [budgets, setBudgets] = useState<BudgetSummary[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [filter, setFilter] = useState<StatusFilter>('all')
  const [deletingId, setDeletingId] = useState<string | null>(null)
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null)
  const [deleteError, setDeleteError] = useState<string | null>(null)
  const [exportingId, setExportingId] = useState<string | null>(null)
  const [exportError, setExportError] = useState<string | null>(null)

  // The list is org-scoped to the acting user — wait for identity to resolve
  // before fetching, exactly like ReportsView (skipping this 401s on a fresh
  // browser where the picker hasn't set the acting user yet).
  const { currentUser, loading: identityLoading, error: identityError } = useActingUser()

  useEffect(() => {
    if (identityLoading) return
    let cancelled = false
    setBudgets(null)
    setError(null)
    if (!currentUser) {
      setError(identityError ?? 'No user available — cannot load budgets.')
      return
    }
    api.listBudgets()
      .then(list => { if (!cancelled) setBudgets(list) })
      .catch(e => { if (!cancelled) setError(e instanceof Error ? e.message : 'Failed to load budgets') })
    return () => { cancelled = true }
  }, [identityLoading, currentUser?.id, identityError])

  const filtered = useMemo(() => {
    if (!budgets) return []
    if (filter === 'all') return budgets
    return budgets.filter(b => b.status === filter)
  }, [budgets, filter])

  const counts = useMemo(() => {
    const c: Record<StatusFilter, number> = { all: 0, draft: 0, final: 0, archived: 0 }
    for (const b of budgets ?? []) {
      c.all += 1
      c[b.status] += 1
    }
    return c
  }, [budgets])

  // Two-step inline delete: first click arms a 4s confirm window, a second
  // click within it deletes. No window.confirm, no modal.
  function handleDeleteClick(id: string) {
    if (confirmDeleteId !== id) {
      setConfirmDeleteId(id)
      window.setTimeout(() => {
        setConfirmDeleteId(current => (current === id ? null : current))
      }, 4000)
      return
    }
    setConfirmDeleteId(null)
    setDeletingId(id)
    setDeleteError(null)
    api.deleteBudget(id)
      .then(() => api.listBudgets())
      .then(list => setBudgets(list))
      .catch(e => setDeleteError(e instanceof Error ? e.message : 'Failed to delete budget'))
      .finally(() => setDeletingId(null))
  }

  async function handleDownloadClick(e: MouseEvent, b: BudgetSummary) {
    e.stopPropagation()
    if (exportingId) return
    setExportingId(b.id)
    setExportError(null)
    try {
      await downloadBudgetDocx(b)
    } catch (err) {
      setExportError(err instanceof Error ? err.message : 'Failed to export Word document')
    } finally {
      setExportingId(null)
    }
  }

  const tabItems = FILTERS.map(f => ({
    id: f.id,
    label: f.label,
    badge: budgets ? (
      <span className="ml-0.5 text-small tabular-nums text-fg-subtle">{counts[f.id]}</span>
    ) : undefined,
  }))

  return (
    // The shell already provides the single <main> landmark.
    <div className="mx-auto w-full max-w-5xl px-4 py-8 sm:px-8 sm:py-10">
      {/* Header row */}
      <div className="mb-6 flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-h1 text-fg">Budgets</h1>
          <p className="mt-1 text-body text-fg-muted">
            Every study budget your organization has built. Open one to keep costing it.
          </p>
        </div>
        <Button onClick={onNew} leftIcon={<Plus aria-hidden />}>
          New budget
        </Button>
      </div>

      {/* Opening a budget failed — non-fatal */}
      {openError ? (
        <Banner tone="danger" title="Couldn't open that budget" onDismiss={onDismissOpenError} className="mb-3">
          {openError}
        </Banner>
      ) : null}

      {/* Delete failed — non-fatal */}
      {deleteError ? (
        <Banner tone="danger" title="Couldn't delete that budget" onDismiss={() => setDeleteError(null)} className="mb-3">
          {deleteError}
        </Banner>
      ) : null}

      {/* Word export failed — non-fatal */}
      {exportError ? (
        <Banner tone="danger" title="Couldn't export that budget" onDismiss={() => setExportError(null)} className="mb-3">
          {exportError}
        </Banner>
      ) : null}

      {/* Fatal error */}
      {error ? (
        <Banner tone="danger" title="Couldn't load your budgets">
          {error}
        </Banner>
      ) : null}

      {/* Loading */}
      {budgets === null && !error ? (
        <div className="space-y-3" role="status" aria-busy="true" aria-label="Loading budgets">
          {[0, 1, 2].map(i => (
            <div key={i} className="h-[76px] animate-pulse rounded-card border border-line bg-surface-muted" />
          ))}
        </div>
      ) : null}

      {/* Empty (no budgets at all) */}
      {budgets?.length === 0 ? (
        <Card padding="none">
          <EmptyState
            icon={<Receipt />}
            title="No budgets yet"
            description="Enter a drug name and get a complete, priced 17-category budget, then edit anything you disagree with."
            action={
              <Button onClick={onNew} leftIcon={<Plus aria-hidden />}>
                Create your first budget
              </Button>
            }
          />
        </Card>
      ) : null}

      {budgets && budgets.length > 0 ? (
        <>
          {/* Status filter */}
          <div className="mb-4">
            <Tabs
              id={TABS_ID}
              variant="pill"
              ariaLabel="Filter budgets by status"
              items={tabItems}
              value={filter}
              onChange={id => setFilter(id as StatusFilter)}
            />
          </div>

          <TabPanel tabsId={TABS_ID} id={filter} value={filter}>
            <p className="sr-only" role="status" aria-live="polite">
              {filtered.length} {filtered.length === 1 ? 'budget' : 'budgets'} shown
            </p>

            {/* Empty (filter hides everything) */}
            {filtered.length === 0 ? (
              <Card padding="none">
                <EmptyState
                  icon={<Search />}
                  title="No budgets match this filter"
                  description="Pick another status to see the rest."
                  action={
                    <Button variant="secondary" onClick={() => setFilter('all')}>
                      Show all budgets
                    </Button>
                  }
                />
              </Card>
            ) : (
              <ul className="space-y-2.5">
                {filtered.map(b => {
                  const isOpening = openingId === b.id
                  const isDeleting = deletingId === b.id
                  const isConfirming = confirmDeleteId === b.id
                  const status = STATUS_BADGE[b.status]
                  // Mouse convenience: a click on the card body opens the budget.
                  // Keyboard and screen-reader users use the explicit Open button.
                  const openFromCard = (e: MouseEvent<HTMLElement>) => {
                    if ((e.target as HTMLElement).closest('button, a')) return
                    if (!isOpening && !isDeleting) onOpen(b)
                  }
                  return (
                    <Card
                      as="li"
                      key={b.id}
                      padding="none"
                      onClick={openFromCard}
                      className={`flex cursor-pointer flex-wrap items-center gap-x-4 gap-y-3 p-4 transition duration-fast hover:border-line-strong hover:shadow-pop ${
                        b.status === 'archived' ? 'opacity-70' : ''
                      }`}
                    >
                      <span
                        aria-hidden
                        className="flex h-11 w-11 flex-none items-center justify-center rounded-xl bg-surface-muted text-fg-muted [&>svg]:h-5 [&>svg]:w-5"
                      >
                        <Receipt />
                      </span>

                      <div className="min-w-0 flex-1 basis-56">
                        <div className="flex flex-wrap items-center gap-2">
                          <h2 className="truncate text-body font-semibold text-fg">{b.name}</h2>
                          <Badge variant={status.variant} dot>
                            {status.label}
                          </Badge>
                          {b.protocol_id ? <Badge variant="info">From protocol</Badge> : null}
                          {b.study_type ? <Badge variant="info">{STUDY_LABELS[b.study_type]}</Badge> : null}
                        </div>
                        <p className="mt-1 truncate text-small text-fg-muted">
                          {b.drug_inn ?? 'No drug linked'}
                          {b.subject_profile ? ` · ${PROFILE_LABEL[b.subject_profile]}` : ''} · {b.category_count} categories · {b.line_item_count} lines · {relativeDate(b.updated_at)}
                        </p>
                      </div>

                      <div className="flex-none text-right">
                        <div className="text-body font-semibold tabular-nums text-fg">
                          {formatMoney(b.grand_total, b.currency)}
                        </div>
                        <div className="text-small text-fg-subtle">Grand total</div>
                      </div>

                      <div className="flex flex-none flex-wrap items-center gap-2">
                        <Button
                          variant="secondary"
                          size="sm"
                          onClick={() => onOpen(b)}
                          loading={isOpening}
                          disabled={isDeleting}
                          rightIcon={<ArrowRight aria-hidden />}
                          aria-label={`Open ${b.name}`}
                        >
                          Open
                        </Button>
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={e => handleDownloadClick(e, b)}
                          loading={exportingId === b.id}
                          disabled={exportingId !== null || isDeleting || isOpening}
                          leftIcon={<Download aria-hidden />}
                          title="Download Word document"
                          aria-label={`Download ${b.name} as a Word document`}
                        >
                          Word
                        </Button>
                        <Button
                          variant={isConfirming ? 'danger' : 'ghost'}
                          size="sm"
                          onClick={() => handleDeleteClick(b.id)}
                          loading={isDeleting}
                          disabled={isOpening}
                          leftIcon={<Trash2 aria-hidden />}
                          aria-label={isConfirming ? `Confirm delete of ${b.name}` : `Delete ${b.name}`}
                        >
                          {isDeleting ? 'Deleting…' : isConfirming ? 'Confirm' : 'Delete'}
                        </Button>
                      </div>
                    </Card>
                  )
                })}
              </ul>
            )}
          </TabPanel>
        </>
      ) : null}
    </div>
  )
}
