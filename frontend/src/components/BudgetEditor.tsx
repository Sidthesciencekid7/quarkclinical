import { useRef, useState } from 'react'
import { api } from '../api/client'
import BudgetDerivationPanel from './BudgetDerivationPanel'
import BudgetSourcesPanel from './BudgetSourcesPanel'
import BudgetSettingsCard from './BudgetSettingsCard'
import BudgetCategorySection from './BudgetCategorySection'
import BudgetRollupCard from './BudgetRollupCard'
import BudgetExportButton from './BudgetExportButton'
import { formatMoney } from './budgetFormat'
import { BUDGET_BUCKET_LABELS } from '../types'
import type { Budget, BudgetBucket, BudgetCategory, BudgetLineItem, BudgetRollup, BudgetStatus } from '../types'

interface Props {
  budget: Budget
  onBack: () => void
  onChanged: (b: Budget) => void
  /** True right after this budget was created — shows the one-time "ready"
   *  hero banner. Reset by the parent on any other navigation. */
  justCreated?: boolean
}

const STATUSES: BudgetStatus[] = ['draft', 'final', 'archived']
const BUCKETS: BudgetBucket[] = ['direct', 'stipend', 'variable', 'pass_through', 'investigator_grants', 'software']

function LeftArrowIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
      <path d="M11 7H3M6 3L2 7l4 4" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}
function PlusIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 15 15" fill="none">
      <path d="M7.5 2v11M2 7.5h11" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
    </svg>
  )
}
function InfoIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 20 20" fill="none" className="flex-shrink-0 mt-0.5 text-slate-400">
      <circle cx="10" cy="10" r="7.25" stroke="currentColor" strokeWidth="1.5" />
      <path d="M10 9v4.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
      <circle cx="10" cy="6.5" r="0.9" fill="currentColor" />
    </svg>
  )
}

export default function BudgetEditor({ budget: initialBudget, onBack, onChanged, justCreated }: Props) {
  const [budget, setBudget] = useState<Budget>(initialBudget)
  const [rollup, setRollup] = useState<BudgetRollup>(initialBudget.rollup)
  const [error, setError] = useState<string | null>(null)
  const [nameDraft, setNameDraft] = useState(initialBudget.name)
  const [addingCategory, setAddingCategory] = useState(false)
  const [newCategoryName, setNewCategoryName] = useState('')
  const [newCategoryBucket, setNewCategoryBucket] = useState<BudgetBucket>('direct')
  const [showReady, setShowReady] = useState(!!justCreated)
  const [expandedIds, setExpandedIds] = useState<Set<string>>(new Set())
  const [sourcesExpanded, setSourcesExpanded] = useState(false)
  const sourcesRef = useRef<HTMLDivElement>(null)

  function openSources() {
    setSourcesExpanded(true)
    // Wait a tick for the panel to actually expand before scrolling to it.
    window.setTimeout(() => sourcesRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' }), 50)
  }

  function toggleCategory(id: string) {
    setExpandedIds(prev => {
      const n = new Set(prev)
      n.has(id) ? n.delete(id) : n.add(id)
      return n
    })
  }
  const allExpanded = expandedIds.size === budget.categories.length && budget.categories.length > 0
  const totalLineCount = budget.categories.reduce((n, c) => n + c.line_items.length, 0)
  const showManualBadge = budget.derivation != null

  // Guards a slow, superseded rollup response from clobbering a fresher one.
  const rollupSeq = useRef(0)

  /** For a budget-level PATCH or link/unlink, which return the WHOLE budget
   *  (rollup included) — just replace state with it directly. */
  function applyBudget(b: Budget) {
    setBudget(b)
    setRollup(b.rollup)
    onChanged(b)
  }

  // ── THE ONE REFRESH RULE ──────────────────────────────────────────────
  // After ANY line-item or category write, the caller splices the object the
  // server returned into local state (so the row/section shows exactly what
  // was stored), then calls this to refresh the rollup. Every money
  // aggregate on screen — grand total, discount, bucket totals, and each
  // category's subtotal via rollup.category_subtotals[cat.id] — reads from
  // `rollup`, never from a category's own (possibly stale) `subtotal` field.
  // One cheap call refreshes all of them.
  async function refreshRollup() {
    const seq = ++rollupSeq.current
    try {
      const r = await api.getBudgetRollup(budget.id)
      if (seq === rollupSeq.current) setRollup(r)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to refresh totals')
    }
  }

  function handleLineChanged(li: BudgetLineItem) {
    setBudget(b => {
      const next: Budget = {
        ...b,
        categories: b.categories.map(c =>
          c.id === li.category_id
            ? { ...c, line_items: c.line_items.map(x => (x.id === li.id ? li : x)) }
            : c
        ),
      }
      onChanged(next)
      return next
    })
  }

  function handleLineDeleted(categoryId: string, id: string) {
    setBudget(b => {
      const next: Budget = {
        ...b,
        categories: b.categories.map(c =>
          c.id === categoryId ? { ...c, line_items: c.line_items.filter(x => x.id !== id) } : c
        ),
      }
      onChanged(next)
      return next
    })
  }

  function handleCategoryChanged(cat: BudgetCategory) {
    setBudget(b => {
      const next: Budget = { ...b, categories: b.categories.map(c => (c.id === cat.id ? cat : c)) }
      onChanged(next)
      return next
    })
  }

  function handleCategoryDeleted(id: string) {
    setBudget(b => {
      const next: Budget = { ...b, categories: b.categories.filter(c => c.id !== id) }
      onChanged(next)
      return next
    })
  }

  // Reordering without a drag library: derive the COMPLETE reordered id array
  // from budget.categories (the contract requires every id exactly once —
  // never build it from a partial slice), swap neighbors, and POST it.
  async function handleMoveCategory(categoryId: string, dir: 'up' | 'down') {
    const ids = budget.categories.map(c => c.id)
    const idx = ids.indexOf(categoryId)
    const swapIdx = dir === 'up' ? idx - 1 : idx + 1
    if (swapIdx < 0 || swapIdx >= ids.length) return
    const reordered = [...ids]
    ;[reordered[idx], reordered[swapIdx]] = [reordered[swapIdx], reordered[idx]]
    try {
      const cats = await api.reorderBudgetCategories(budget.id, reordered)
      setBudget(b => {
        const next = { ...b, categories: cats }
        onChanged(next)
        return next
      })
      refreshRollup()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to reorder categories')
    }
  }

  async function handleAddCategory() {
    const trimmed = newCategoryName.trim()
    if (!trimmed) return
    try {
      const created = await api.createBudgetCategory(budget.id, { name: trimmed, bucket: newCategoryBucket })
      setBudget(b => {
        const next = { ...b, categories: [...b.categories, created] }
        onChanged(next)
        return next
      })
      setNewCategoryName('')
      setAddingCategory(false)
      refreshRollup()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to add category')
    }
  }

  async function commitName() {
    const trimmed = nameDraft.trim()
    if (!trimmed || trimmed === budget.name) { setNameDraft(budget.name); return }
    try {
      const updated = await api.updateBudget(budget.id, { name: trimmed })
      applyBudget(updated)
    } catch (e) {
      setNameDraft(budget.name)
      setError(e instanceof Error ? e.message : 'Failed to rename budget')
    }
  }

  async function handleStatusChange(status: BudgetStatus) {
    try {
      const updated = await api.updateBudget(budget.id, { status })
      applyBudget(updated)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to change status')
    }
  }

  return (
    <main className="app-canvas min-h-[calc(100vh-57px)]">
      <div className="max-w-5xl mx-auto px-5 py-10 space-y-5 fade-up">
        <button onClick={onBack} className="btn-ghost">
          <LeftArrowIcon />
          All budgets
        </button>

        {error && (
          <div className="rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700 flex items-start justify-between gap-3">
            <span>{error}</span>
            <button onClick={() => setError(null)} aria-label="Dismiss" className="flex-shrink-0 text-red-400 hover:text-red-600 cursor-pointer font-bold leading-none">
              ×
            </button>
          </div>
        )}

        <div className="flex items-center justify-between gap-4 flex-wrap">
          <div className="flex items-center gap-2.5 flex-wrap min-w-0">
            <input
              value={nameDraft}
              onChange={e => setNameDraft(e.target.value)}
              onBlur={commitName}
              onKeyDown={e => {
                if (e.key === 'Enter') (e.target as HTMLInputElement).blur()
                if (e.key === 'Escape') setNameDraft(budget.name)
              }}
              aria-label="Budget name"
              className="text-2xl font-extrabold text-slate-900 tracking-tight bg-transparent border border-transparent hover:border-slate-200 focus:border-cyan-400 focus:ring-2 focus:ring-cyan-500/40 rounded-lg px-2 py-1 min-w-[220px]"
            />
            {budget.protocol_id && (
              <span className="text-[10px] font-bold text-cyan-700 bg-cyan-50 border border-cyan-100 px-1.5 py-0.5 rounded-full whitespace-nowrap">
                From protocol
              </span>
            )}
          </div>
          <div className="flex items-center gap-2 flex-shrink-0">
            <BudgetExportButton budget={budget} />
            <select
              value={budget.status}
              onChange={e => handleStatusChange(e.target.value as BudgetStatus)}
              aria-label="Budget status"
              className="text-xs font-semibold text-slate-600 bg-white border border-slate-200 rounded-lg px-2.5 py-1.5 cursor-pointer capitalize focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-400/60"
            >
              {STATUSES.map(s => <option key={s} value={s} className="capitalize">{s}</option>)}
            </select>
          </div>
        </div>

        {showReady && (
          <div className="rounded-2xl border border-cyan-200 bg-cyan-50/60 px-5 py-4 flex items-center justify-between gap-4 flex-wrap fade-up">
            <div>
              <p className="text-sm font-bold text-slate-900">
                Your budget is ready — {formatMoney(rollup.grand_total, budget.currency)}
              </p>
              <p className="text-xs text-slate-600 mt-0.5">
                {budget.categories.length} categories, {totalLineCount} line items, every figure priced. Download it,
                or edit anything below first.
              </p>
            </div>
            <div className="flex items-center gap-2 flex-shrink-0">
              <BudgetExportButton budget={budget} variant="hero" />
              <button
                onClick={() => setShowReady(false)}
                aria-label="Dismiss"
                className="text-slate-400 hover:text-slate-600 cursor-pointer font-bold leading-none px-1"
              >
                ×
              </button>
            </div>
          </div>
        )}

        <p className="flex items-start gap-2 text-[12px] leading-relaxed text-slate-500 bg-slate-50 border border-slate-200 rounded-xl px-4 py-2.5">
          <InfoIcon />
          <span>
            {budget.derivation?.disclaimer ||
              'Every figure here is an estimate from our CRO rate card. Edit any number — the totals and the Word document follow.'}
          </span>
        </p>

        <BudgetDerivationPanel budget={budget} onBudgetUpdated={applyBudget} onError={setError} />

        <div ref={sourcesRef}>
          <BudgetSourcesPanel budget={budget} expanded={sourcesExpanded} onToggle={() => setSourcesExpanded(s => !s)} />
        </div>

        <BudgetSettingsCard budget={budget} onBudgetUpdated={applyBudget} onError={setError} />

        <div className="flex items-center justify-between gap-3">
          <p className="text-xs font-semibold text-slate-500">
            {budget.categories.length} categories · {totalLineCount} line items
          </p>
          <button
            onClick={() => setExpandedIds(allExpanded ? new Set() : new Set(budget.categories.map(c => c.id)))}
            className="btn-ghost text-xs py-1.5 px-3"
          >
            {allExpanded ? 'Collapse all' : 'Expand all'}
          </button>
        </div>

        <div className="space-y-4">
          {budget.categories.map((cat, i) => (
            <BudgetCategorySection
              key={cat.id}
              budget={budget}
              category={cat}
              subtotal={rollup.category_subtotals[cat.id] ?? cat.subtotal}
              isFirst={i === 0}
              isLast={i === budget.categories.length - 1}
              expanded={expandedIds.has(cat.id)}
              onToggle={() => toggleCategory(cat.id)}
              showManualBadge={showManualBadge}
              onLineChanged={handleLineChanged}
              onLineDeleted={id => handleLineDeleted(cat.id, id)}
              onCategoryChanged={handleCategoryChanged}
              onCategoryDeleted={handleCategoryDeleted}
              onMove={dir => handleMoveCategory(cat.id, dir)}
              onDirty={refreshRollup}
              onError={setError}
            />
          ))}
        </div>

        {addingCategory ? (
          <div className="card p-4 flex items-center gap-3 flex-wrap">
            <input
              value={newCategoryName}
              onChange={e => setNewCategoryName(e.target.value)}
              placeholder="Category name"
              autoFocus
              className="input-base flex-1 min-w-[200px]"
            />
            <select
              value={newCategoryBucket}
              onChange={e => setNewCategoryBucket(e.target.value as BudgetBucket)}
              className="input-base w-auto cursor-pointer"
            >
              {BUCKETS.map(b => <option key={b} value={b}>{BUDGET_BUCKET_LABELS[b]}</option>)}
            </select>
            <button onClick={handleAddCategory} disabled={!newCategoryName.trim()} className="btn-primary text-xs py-2 px-4">
              Add
            </button>
            <button onClick={() => { setAddingCategory(false); setNewCategoryName('') }} className="btn-ghost text-xs py-2 px-4">
              Cancel
            </button>
          </div>
        ) : (
          <button onClick={() => setAddingCategory(true)} className="btn-ghost">
            <PlusIcon />
            Add category
          </button>
        )}

        <BudgetRollupCard budget={budget} rollup={rollup} variant="full" onBudgetUpdated={applyBudget} onError={setError} onOpenSources={openSources} />
      </div>

      <BudgetRollupCard budget={budget} rollup={rollup} variant="sticky" />
    </main>
  )
}
