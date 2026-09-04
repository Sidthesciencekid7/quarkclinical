import { useState } from 'react'
import { api } from '../api/client'
import BudgetLineItemRow from './BudgetLineItemRow'
import { formatMoney } from './budgetFormat'
import { BUDGET_BUCKET_LABELS } from '../types'
import type { Budget, BudgetBucket, BudgetCategory, BudgetLineItem } from '../types'

interface Props {
  budget: Budget
  category: BudgetCategory
  /** Read from rollup.category_subtotals — NEVER category.subtotal, which
   *  goes stale the instant a line item changes. */
  subtotal: number
  isFirst: boolean
  isLast: boolean
  /** Collapse state lives in BudgetEditor so it survives this section's
   *  re-render and Expand all / Collapse all can control every section. */
  expanded: boolean
  onToggle: () => void
  /** Only mark a hand-added row "Yours" in an auto-populated budget — in a
   *  legacy hand-built one every row would be badged, which is noise. */
  showManualBadge: boolean
  onLineChanged: (li: BudgetLineItem) => void
  onLineDeleted: (id: string) => void
  onCategoryChanged: (c: BudgetCategory) => void
  onCategoryDeleted: (id: string) => void
  onMove: (dir: 'up' | 'down') => void
  onDirty: () => void
  onError: (msg: string) => void
}

const BUCKETS: BudgetBucket[] = ['direct', 'stipend', 'variable', 'pass_through']

function ChevronUpIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 20 20" fill="none">
      <path d="M5 12l5-5 5 5" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}
function ChevronDownIcon({ className }: { className?: string }) {
  return (
    <svg width="14" height="14" viewBox="0 0 20 20" fill="none" className={className}>
      <path d="M5 8l5 5 5-5" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}
function TrashIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 20 20" fill="none">
      <path d="M4 6h12M8 6V4.5A1.5 1.5 0 019.5 3h1A1.5 1.5 0 0112 4.5V6m-6.5 0l.6 10.2a1.5 1.5 0 001.5 1.4h4.8a1.5 1.5 0 001.5-1.4L14.5 6" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
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

export default function BudgetCategorySection({
  budget, category, subtotal, isFirst, isLast, expanded, onToggle, showManualBadge,
  onLineChanged, onLineDeleted, onCategoryChanged, onCategoryDeleted, onMove, onDirty, onError,
}: Props) {
  const [nameDraft, setNameDraft] = useState(category.name)
  const [savingName, setSavingName] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [addingLine, setAddingLine] = useState(false)
  const [focusNewId, setFocusNewId] = useState<string | null>(null)

  const nonCostedCount = category.line_items.filter(li => li.status !== 'costed').length

  async function commitName() {
    const trimmed = nameDraft.trim()
    if (!trimmed || trimmed === category.name) {
      setNameDraft(category.name)
      return
    }
    setSavingName(true)
    try {
      const updated = await api.updateBudgetCategory(budget.id, category.id, { name: trimmed })
      onCategoryChanged({ ...updated, line_items: category.line_items })
    } catch (e) {
      setNameDraft(category.name)
      onError(e instanceof Error ? e.message : 'Failed to rename category')
    } finally {
      setSavingName(false)
    }
  }

  async function handleBucketChange(bucket: BudgetBucket) {
    try {
      const updated = await api.updateBudgetCategory(budget.id, category.id, { bucket })
      onCategoryChanged({ ...updated, line_items: category.line_items })
      onDirty()
    } catch (e) {
      onError(e instanceof Error ? e.message : 'Failed to change bucket')
    }
  }

  function handleDeleteClick() {
    if (!confirmDelete) {
      setConfirmDelete(true)
      window.setTimeout(() => setConfirmDelete(false), 4000)
      return
    }
    setConfirmDelete(false)
    api.deleteBudgetCategory(budget.id, category.id)
      .then(() => { onCategoryDeleted(category.id); onDirty() })
      .catch(e => onError(e instanceof Error ? e.message : 'Failed to delete category'))
  }

  async function handleAddLine() {
    if (!expanded) onToggle()
    setAddingLine(true)
    try {
      const created = await api.createBudgetLineItem(budget.id, category.id, {
        label: 'New line item',
        status: 'costed',
      })
      onCategoryChanged({ ...category, line_items: [...category.line_items, created] })
      setFocusNewId(created.id)
      onDirty()
    } catch (e) {
      onError(e instanceof Error ? e.message : 'Failed to add line item')
    } finally {
      setAddingLine(false)
    }
  }

  // Reordering without a drag library: build the COMPLETE reordered id array
  // from the full line_items list (the contract requires every id exactly
  // once), swap the two neighbors, and POST it.
  async function handleReorderLine(itemId: string, dir: 'up' | 'down') {
    const ids = category.line_items.map(li => li.id)
    const idx = ids.indexOf(itemId)
    const swapIdx = dir === 'up' ? idx - 1 : idx + 1
    if (swapIdx < 0 || swapIdx >= ids.length) return
    const reordered = [...ids]
    ;[reordered[idx], reordered[swapIdx]] = [reordered[swapIdx], reordered[idx]]
    try {
      const items = await api.reorderBudgetLineItems(budget.id, category.id, reordered)
      onCategoryChanged({ ...category, line_items: items })
      onDirty()
    } catch (e) {
      onError(e instanceof Error ? e.message : 'Failed to reorder line items')
    }
  }

  return (
    <section className="card overflow-hidden">
      <div
        className="px-5 py-3.5 border-b border-slate-100 flex items-center gap-3 flex-wrap cursor-pointer"
        onClick={e => {
          if ((e.target as HTMLElement).closest('input,select,button')) return
          onToggle()
        }}
      >
        <button
          onClick={onToggle}
          aria-expanded={expanded}
          aria-controls={`cat-${category.id}`}
          aria-label={expanded ? `Collapse ${category.name}` : `Expand ${category.name}`}
          className="p-1 rounded-lg text-slate-400 hover:text-slate-700 hover:bg-slate-100 cursor-pointer flex-shrink-0 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-400/60"
        >
          <ChevronDownIcon className={`transition-transform ${expanded ? 'rotate-180' : ''}`} />
        </button>

        <input
          value={nameDraft}
          onChange={e => setNameDraft(e.target.value)}
          onBlur={commitName}
          onKeyDown={e => {
            if (e.key === 'Enter') (e.target as HTMLInputElement).blur()
            if (e.key === 'Escape') setNameDraft(category.name)
          }}
          disabled={savingName}
          aria-label="Category name"
          className="font-bold text-slate-900 bg-transparent border border-transparent hover:border-slate-200 focus:border-cyan-400 focus:ring-2 focus:ring-cyan-500/40 rounded-lg px-2 py-1 flex-1 min-w-[160px]"
        />

        <select
          value={category.bucket}
          onChange={e => handleBucketChange(e.target.value as BudgetBucket)}
          aria-label="Category bucket"
          className="text-xs font-semibold text-slate-600 bg-slate-100 border border-slate-200 rounded-lg px-2 py-1 cursor-pointer focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-400/60"
        >
          {BUCKETS.map(b => <option key={b} value={b}>{BUDGET_BUCKET_LABELS[b]}</option>)}
        </select>

        <div className="text-[11px] text-slate-400 flex-shrink-0">{category.line_items.length} items</div>

        <div className="text-right">
          <div className="font-bold text-slate-900 tabular-nums">{formatMoney(subtotal, budget.currency)}</div>
          <div className="text-[11px] text-slate-400">Subtotal</div>
        </div>

        <div className="flex items-center gap-1 flex-shrink-0">
          <button
            onClick={() => onMove('up')}
            disabled={isFirst}
            aria-label={`Move ${category.name} up`}
            className="p-1.5 rounded-lg text-slate-400 hover:text-slate-700 hover:bg-slate-100 disabled:opacity-30 disabled:cursor-not-allowed cursor-pointer focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-400/60"
          >
            <ChevronUpIcon />
          </button>
          <button
            onClick={() => onMove('down')}
            disabled={isLast}
            aria-label={`Move ${category.name} down`}
            className="p-1.5 rounded-lg text-slate-400 hover:text-slate-700 hover:bg-slate-100 disabled:opacity-30 disabled:cursor-not-allowed cursor-pointer focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-400/60"
          >
            <ChevronDownIcon />
          </button>
          <button
            onClick={handleDeleteClick}
            aria-label={confirmDelete ? `Confirm delete of ${category.name} — its line items go with it` : `Delete ${category.name}`}
            title={confirmDelete ? 'This deletes its line items too' : 'Delete category'}
            className={`p-1.5 rounded-lg cursor-pointer focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-400/60 ${
              confirmDelete ? 'text-red-600 bg-red-50' : 'text-slate-400 hover:text-red-600 hover:bg-red-50'
            }`}
          >
            <TrashIcon />
          </button>
        </div>
      </div>

      {expanded && (
        <>
          <div className="overflow-x-auto" id={`cat-${category.id}`}>
            <table className="w-full min-w-[720px] text-sm">
              <thead>
                <tr className="text-[11px] font-semibold uppercase tracking-wider text-slate-400 bg-slate-50/70">
                  <th className="text-left px-5 py-2 font-semibold">Line item</th>
                  <th className="text-right px-2 py-2 font-semibold" style={{ width: 80 }}>Qty</th>
                  <th className="text-right px-2 py-2 font-semibold" style={{ width: 110 }}>Unit cost</th>
                  <th className="text-left px-2 py-2 font-semibold" style={{ width: 120 }}>Unit</th>
                  <th className="text-right px-2 py-2 font-semibold" style={{ width: 130 }}>Total</th>
                  <th className="text-left px-2 py-2 font-semibold" style={{ width: 130 }}>Status</th>
                  <th className="px-2 py-2" style={{ width: 56 }} />
                </tr>
              </thead>
              <tbody>
                {category.line_items.map((item, i) => (
                  <BudgetLineItemRow
                    key={item.id}
                    budgetId={budget.id}
                    item={item}
                    currency={budget.currency}
                    derivation={budget.derivation}
                    isFirst={i === 0}
                    isLast={i === category.line_items.length - 1}
                    autoFocusLabel={focusNewId === item.id}
                    showManualBadge={showManualBadge}
                    onChanged={onLineChanged}
                    onDeleted={() => onLineDeleted(item.id)}
                    onMove={dir => handleReorderLine(item.id, dir)}
                    onDirty={onDirty}
                    onError={onError}
                  />
                ))}
                {category.line_items.length === 0 && (
                  <tr>
                    <td colSpan={7} className="px-5 py-6 text-center text-sm text-slate-400">
                      No line items — this category will still appear in the Word document with a zero subtotal.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>

          <div className="px-5 py-3 border-t border-slate-100 flex items-center justify-between gap-3 flex-wrap">
            <button onClick={handleAddLine} disabled={addingLine} className="btn-ghost text-xs py-1.5 px-3">
              <PlusIcon />
              {addingLine ? 'Adding…' : 'Add line item'}
            </button>
            {nonCostedCount > 0 && (
              <p className="text-[11px] text-slate-400">
                {nonCostedCount} of {category.line_items.length} rows carry no cost (see Status) and are excluded from
                this subtotal — they still appear in the Word document.
              </p>
            )}
          </div>
        </>
      )}
    </section>
  )
}
