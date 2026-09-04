import { useEffect, useMemo, useState } from 'react'
import type { MouseEvent } from 'react'
import { api } from '../api/client'
import { useActingUser } from '../lib/actingUser'
import { formatMoney, relativeDate } from './budgetFormat'
import { downloadBudgetDocx } from './BudgetExportButton'
import type { BudgetStatus, BudgetSummary, StudyType } from '../types'

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

const STUDY_BADGE: Record<StudyType, string> = {
  BE: 'bg-cyan-50 text-cyan-700 border-cyan-200',
  biosimilar_pk: 'bg-violet-50 text-violet-700 border-violet-200',
  food_effect: 'bg-amber-50 text-amber-700 border-amber-200',
}

const STATUS_PILL: Record<BudgetStatus, string> = {
  draft: 'bg-slate-100 text-slate-600 border-slate-200',
  final: 'bg-emerald-50 text-emerald-700 border-emerald-200',
  archived: 'bg-slate-100 text-slate-400 border-slate-200',
}

type StatusFilter = 'all' | BudgetStatus

const FILTERS: { id: StatusFilter; label: string }[] = [
  { id: 'all', label: 'All' },
  { id: 'draft', label: 'Draft' },
  { id: 'final', label: 'Final' },
  { id: 'archived', label: 'Archived' },
]

/** Same path data as SuiteHome's BudgetIcon — kept local per this codebase's
 *  convention of declaring small icons in the file that uses them. */
function WalletIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 20 20" fill="none">
      <rect x="2" y="5" width="16" height="11" rx="2" stroke="currentColor" strokeWidth="1.5" />
      <path d="M2 8.5h16" stroke="currentColor" strokeWidth="1.5" />
      <circle cx="14.5" cy="12" r="1.25" fill="currentColor" />
    </svg>
  )
}

function Spinner({ size = 16 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none" className="animate-spin">
      <circle cx="8" cy="8" r="6" stroke="#e0f2fe" strokeWidth="2.5" />
      <path d="M8 2a6 6 0 016 6" stroke="#0891b2" strokeWidth="2.5" strokeLinecap="round" />
    </svg>
  )
}

// Same glyphs as ExportButton.tsx / BudgetExportButton.tsx, kept local per
// this codebase's convention of declaring small icons in the file that uses
// them — this row action is a plain slate icon button, not the cyan-filled
// primary action, so it doesn't reuse BudgetExportButton's own markup.
function DownloadIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 12 12" fill="none">
      <path d="M6 2v6M3.5 5.5L6 8l2.5-2.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M2 10h8" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  )
}
function SpinIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 12 12" fill="none" className="animate-spin">
      <circle cx="6" cy="6" r="5" stroke="#e0f2fe" strokeWidth="2" />
      <path d="M6 1a5 5 0 015 5" stroke="#0891b2" strokeWidth="2" strokeLinecap="round" />
    </svg>
  )
}

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

  return (
    <main className="app-canvas min-h-[calc(100vh-57px)]">
      <div className="max-w-5xl mx-auto px-5 py-10 fade-up">
        {/* Header row */}
        <div className="flex items-end justify-between mb-6 flex-wrap gap-3">
          <div>
            <h1 className="text-2xl font-extrabold text-slate-900 tracking-tight">My Budgets</h1>
            <p className="text-sm text-slate-500 mt-1">
              Every study budget your organization has built. Open one to keep costing it.
            </p>
          </div>
          <button onClick={onNew} className="btn-primary">
            <svg width="15" height="15" viewBox="0 0 15 15" fill="none">
              <path d="M7.5 2v11M2 7.5h11" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
            </svg>
            New Budget
          </button>
        </div>

        {/* Status filter */}
        {budgets && budgets.length > 0 && (
          <div className="flex items-center gap-1.5 mb-5">
            {FILTERS.map(f => (
              <button
                key={f.id}
                onClick={() => setFilter(f.id)}
                className={`text-xs font-semibold px-3 py-1.5 rounded-lg border transition-colors cursor-pointer ${
                  filter === f.id
                    ? 'bg-cyan-50 text-cyan-700 border-cyan-200'
                    : 'text-slate-500 border-slate-200 hover:bg-slate-50'
                }`}
              >
                {f.label}
              </button>
            ))}
          </div>
        )}

        {/* Loading */}
        {budgets === null && !error && (
          <div className="space-y-3">
            {[0, 1, 2].map(i => (
              <div key={i} className="h-[76px] rounded-2xl border border-slate-200 bg-white/60 animate-pulse" />
            ))}
          </div>
        )}

        {/* Fatal error */}
        {error && (
          <div className="rounded-2xl border border-red-200 bg-red-50 p-5 text-sm text-red-700">
            Couldn&apos;t load your budgets: {error}
          </div>
        )}

        {/* Opening a budget failed — non-fatal */}
        {openError && (
          <div className="mb-3 rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700 flex items-start justify-between gap-3">
            <span>Couldn&apos;t open that budget: {openError}</span>
            <button
              onClick={onDismissOpenError}
              aria-label="Dismiss"
              className="flex-shrink-0 text-red-400 hover:text-red-600 cursor-pointer font-bold leading-none"
            >
              ×
            </button>
          </div>
        )}

        {/* Delete failed — non-fatal */}
        {deleteError && (
          <div className="mb-3 rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
            Couldn&apos;t delete that budget: {deleteError}
          </div>
        )}

        {/* Word export failed — non-fatal */}
        {exportError && (
          <div className="mb-3 rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
            Couldn&apos;t export that budget: {exportError}
          </div>
        )}

        {/* Empty (no budgets at all) */}
        {budgets?.length === 0 && (
          <div className="rounded-2xl border border-slate-200 bg-white p-12 text-center shadow-sm">
            <div className="w-14 h-14 rounded-2xl bg-cyan-50 border border-cyan-100 text-cyan-500 flex items-center justify-center mx-auto mb-4">
              <WalletIcon />
            </div>
            <h3 className="text-lg font-bold text-slate-900">No budgets yet</h3>
            <p className="text-sm text-slate-500 mt-1.5 max-w-sm mx-auto">
              Enter a drug name and get a complete, priced 17-category budget — then edit anything you disagree with.
            </p>
            <button
              onClick={onNew}
              className="mt-5 inline-flex items-center gap-2 px-5 py-2.5 rounded-xl bg-cyan-600 hover:bg-cyan-700 text-white text-sm font-bold cursor-pointer transition-colors"
            >
              Create your first budget
            </button>
          </div>
        )}

        {/* Empty (filter hides everything) */}
        {budgets && budgets.length > 0 && filtered.length === 0 && (
          <div className="rounded-2xl border border-slate-200 bg-white p-10 text-center text-sm text-slate-500">
            No budgets match this filter.
          </div>
        )}

        {/* List */}
        {filtered.length > 0 && (
          <div className="space-y-2.5">
            {filtered.map(b => {
              const isOpening = openingId === b.id
              const isDeleting = deletingId === b.id
              const isConfirming = confirmDeleteId === b.id
              return (
                <div
                  key={b.id}
                  className={`section-card w-full flex items-center gap-4 px-5 py-4 group ${
                    b.status === 'archived' ? 'opacity-70' : ''
                  }`}
                >
                  <button
                    onClick={() => onOpen(b)}
                    disabled={isOpening || isDeleting}
                    className="flex-1 min-w-0 text-left flex items-center gap-4 disabled:opacity-70 cursor-pointer"
                  >
                    <span className="flex-shrink-0 w-10 h-10 rounded-xl bg-gradient-to-br from-cyan-500 to-teal-600 text-white flex items-center justify-center shadow-sm shadow-cyan-600/25">
                      <WalletIcon />
                    </span>

                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2 flex-wrap">
                        <span className="font-bold text-slate-900 truncate">{b.name}</span>
                        <span className={`text-[10px] font-bold border px-1.5 py-0.5 rounded-md capitalize ${STATUS_PILL[b.status]}`}>
                          {b.status}
                        </span>
                        {b.protocol_id && (
                          <span className="text-[10px] font-bold text-cyan-700 bg-cyan-50 border border-cyan-100 px-1.5 py-0.5 rounded-full whitespace-nowrap">
                            From protocol
                          </span>
                        )}
                        {b.study_type && (
                          <span className={`text-[10px] font-bold border px-1.5 py-0.5 rounded-md ${STUDY_BADGE[b.study_type]}`}>
                            {STUDY_LABELS[b.study_type]}
                          </span>
                        )}
                      </div>
                      <p className="text-xs text-slate-500 mt-1 font-medium truncate">
                        {b.drug_inn ?? 'No drug linked'} · {b.category_count} categories · {b.line_item_count} lines · {relativeDate(b.updated_at)}
                      </p>
                    </div>

                    <div className="flex-shrink-0 text-right">
                      <div className="text-base font-bold text-slate-900 tabular-nums">{formatMoney(b.grand_total, b.currency)}</div>
                      <div className="text-[11px] text-slate-400">Grand total</div>
                    </div>

                    <span className="flex-shrink-0 text-slate-400 group-hover:text-cyan-600 transition-colors flex items-center gap-1.5 text-xs font-semibold">
                      {isOpening ? (
                        <Spinner />
                      ) : (
                        <>
                          Open
                          <svg width="14" height="14" viewBox="0 0 14 14" fill="none" className="transition-transform group-hover:translate-x-0.5">
                            <path d="M3 7h8M7 3l4 4-4 4" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" />
                          </svg>
                        </>
                      )}
                    </span>
                  </button>

                  <button
                    onClick={e => handleDownloadClick(e, b)}
                    disabled={exportingId === b.id || isDeleting || isOpening}
                    title="Download Word document"
                    aria-label={`Download ${b.name} as a Word document`}
                    className="flex-shrink-0 p-1.5 rounded-lg text-slate-400 hover:text-cyan-600 hover:bg-cyan-50 cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
                  >
                    {exportingId === b.id ? <SpinIcon /> : <DownloadIcon />}
                  </button>

                  <button
                    onClick={() => handleDeleteClick(b.id)}
                    disabled={isDeleting || isOpening}
                    className={`flex-shrink-0 inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg border text-xs font-semibold transition-colors cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed ${
                      isConfirming
                        ? 'border-red-300 text-red-700 bg-red-50'
                        : 'border-slate-200 text-slate-500 hover:text-red-700 hover:border-red-200 hover:bg-red-50'
                    }`}
                  >
                    {isDeleting && <Spinner size={13} />}
                    {isDeleting ? 'Deleting…' : isConfirming ? 'Confirm' : 'Delete'}
                  </button>
                </div>
              )
            })}
          </div>
        )}
      </div>
    </main>
  )
}
