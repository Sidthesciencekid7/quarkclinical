import { useRef, useState } from 'react'
import { api } from '../api/client'
import BudgetDerivationPanel from './BudgetDerivationPanel'
import BudgetSourcesPanel from './BudgetSourcesPanel'
import BudgetSettingsCard from './BudgetSettingsCard'
import BudgetCategorySection from './BudgetCategorySection'
import BudgetRollupCard from './BudgetRollupCard'
import BudgetExportButton from './BudgetExportButton'
import { formatMoney } from './budgetFormat'
import { Badge, Banner, Button, Card, ChevronLeft, Input, PillNav, Plus, Select } from '../ui'
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

const STATUS_ITEMS = STATUSES.map(s => ({ id: s, label: s.charAt(0).toUpperCase() + s.slice(1) }))

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
    // The shell already provides the single <main> landmark.
    <div>
      <div className="mx-auto w-full max-w-5xl space-y-5 px-4 py-8 sm:px-8 sm:py-10">
        <Button variant="ghost" size="sm" leftIcon={<ChevronLeft aria-hidden />} onClick={onBack}>
          All budgets
        </Button>

        {error && (
          <Banner tone="danger" onDismiss={() => setError(null)}>
            {error}
          </Banner>
        )}

        <div className="flex flex-wrap items-center justify-between gap-4">
          <div className="flex min-w-0 flex-1 basis-72 flex-wrap items-center gap-2.5">
            <input
              value={nameDraft}
              onChange={e => setNameDraft(e.target.value)}
              onBlur={commitName}
              onKeyDown={e => {
                if (e.key === 'Enter') (e.target as HTMLInputElement).blur()
                if (e.key === 'Escape') setNameDraft(budget.name)
              }}
              aria-label="Budget name"
              className="min-w-0 max-w-full flex-1 basis-56 rounded-input border border-transparent bg-transparent px-2 py-1 text-h1 text-fg hover:border-line-strong focus:border-[color:var(--ring)] focus:outline-none focus:ring-2 focus:ring-[color:var(--ring)]"
            />
            {budget.protocol_id && <Badge variant="info">From protocol</Badge>}
          </div>
          <div className="flex flex-none flex-wrap items-center gap-2">
            <PillNav
              ariaLabel="Budget status"
              size="sm"
              items={STATUS_ITEMS}
              value={budget.status}
              onChange={id => handleStatusChange(id as BudgetStatus)}
            />
            <BudgetExportButton budget={budget} />
          </div>
        </div>

        {showReady && (
          <Banner
            tone="success"
            title={`Your budget is ready — ${formatMoney(rollup.grand_total, budget.currency)}`}
            action={<BudgetExportButton budget={budget} variant="hero" />}
            onDismiss={() => setShowReady(false)}
          >
            {budget.categories.length} categories, {totalLineCount} line items, every figure priced. Download it,
            or edit anything below first.
          </Banner>
        )}

        {/* The one disclaimer that must never be missed: every figure is an estimate. */}
        <Banner tone="warning">
          {budget.derivation?.disclaimer ||
            'Every figure here is an estimate from our CRO rate card. Edit any number — the totals and the Word document follow.'}
        </Banner>

        <BudgetDerivationPanel budget={budget} onBudgetUpdated={applyBudget} onError={setError} />

        <div ref={sourcesRef}>
          <BudgetSourcesPanel budget={budget} expanded={sourcesExpanded} onToggle={() => setSourcesExpanded(s => !s)} />
        </div>

        <BudgetSettingsCard budget={budget} onBudgetUpdated={applyBudget} onError={setError} />

        <div className="flex items-center justify-between gap-3">
          <p className="text-small font-medium tabular-nums text-fg-muted">
            {budget.categories.length} categories · {totalLineCount} line items
          </p>
          <Button
            variant="secondary"
            size="sm"
            onClick={() => setExpandedIds(allExpanded ? new Set() : new Set(budget.categories.map(c => c.id)))}
          >
            {allExpanded ? 'Collapse all' : 'Expand all'}
          </Button>
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
          <Card padding="sm" className="flex flex-wrap items-center gap-3">
            <div className="min-w-[200px] flex-1">
              <Input
                value={newCategoryName}
                onChange={e => setNewCategoryName(e.target.value)}
                placeholder="Category name"
                aria-label="New category name"
                autoFocus
              />
            </div>
            <div className="w-48">
              <Select
                value={newCategoryBucket}
                onChange={e => setNewCategoryBucket(e.target.value as BudgetBucket)}
                aria-label="New category bucket"
              >
                {BUCKETS.map(b => <option key={b} value={b}>{BUDGET_BUCKET_LABELS[b]}</option>)}
              </Select>
            </div>
            <Button onClick={handleAddCategory} disabled={!newCategoryName.trim()}>
              Add
            </Button>
            <Button variant="ghost" onClick={() => { setAddingCategory(false); setNewCategoryName('') }}>
              Cancel
            </Button>
          </Card>
        ) : (
          <Button variant="secondary" leftIcon={<Plus aria-hidden />} onClick={() => setAddingCategory(true)}>
            Add category
          </Button>
        )}

        <BudgetRollupCard budget={budget} rollup={rollup} variant="full" onBudgetUpdated={applyBudget} onError={setError} onOpenSources={openSources} />
      </div>

      <BudgetRollupCard budget={budget} rollup={rollup} variant="sticky" />
    </div>
  )
}
