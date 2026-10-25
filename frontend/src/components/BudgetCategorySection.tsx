import { useState } from 'react'
import { api } from '../api/client'
import BudgetLineItemRow from './BudgetLineItemRow'
import { formatMoney } from './budgetFormat'
import { BUDGET_BUCKET_LABELS } from '../types'
import type { Budget, BudgetBucket, BudgetCategory, BudgetLineItem } from '../types'
import { Button, Card, ChevronDown, IconButton, Plus, Trash2 } from '../ui'

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

const BUCKETS: BudgetBucket[] = ['direct', 'stipend', 'variable', 'pass_through', 'investigator_grants', 'software']

// Distinguishes the six buckets at a glance down a 22-category patient-study
// page. `direct`/`stipend`/`variable`/`pass_through` keep the original quiet
// neutral treatment; the two patient-study buckets get their own tone so they
// read as a different KIND of row, not just another category.
const BUCKET_CHIP: Record<BudgetBucket, string> = {
  direct: 'border-line bg-surface-muted text-fg-muted',
  stipend: 'border-line bg-surface-muted text-fg-muted',
  variable: 'border-line bg-surface-muted text-fg-muted',
  pass_through: 'border-line bg-surface-muted text-fg-muted',
  investigator_grants: 'border-[color:var(--ok-border)] bg-[var(--ok-bg)] text-[color:var(--ok-fg)]',
  software: 'border-[color:var(--info-border)] bg-[var(--info-bg)] text-[color:var(--info-fg)]',
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
    <Card as="section" padding="none" className="overflow-hidden">
      <div
        className="flex cursor-pointer flex-wrap items-center gap-3 border-b border-line px-4 py-3 sm:px-5"
        onClick={e => {
          if ((e.target as HTMLElement).closest('input,select,button')) return
          onToggle()
        }}
      >
        <IconButton
          size="sm"
          label={expanded ? `Collapse ${category.name}` : `Expand ${category.name}`}
          icon={<ChevronDown className={`transition-transform duration-fast ${expanded ? 'rotate-180' : ''}`} />}
          onClick={onToggle}
          aria-expanded={expanded}
          aria-controls={`cat-${category.id}`}
          className="flex-none"
        />

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
          className="min-w-[160px] flex-1 rounded-input border border-transparent bg-transparent px-2 py-1 text-body font-semibold text-fg hover:border-line-strong focus:border-[color:var(--ring)] focus:outline-none focus:ring-2 focus:ring-[color:var(--ring)] disabled:opacity-60"
        />

        <select
          value={category.bucket}
          onChange={e => handleBucketChange(e.target.value as BudgetBucket)}
          aria-label="Category bucket"
          className={`h-8 cursor-pointer rounded-full border px-3 text-small font-medium ${BUCKET_CHIP[category.bucket]}`}
        >
          {BUCKETS.map(b => <option key={b} value={b}>{BUDGET_BUCKET_LABELS[b]}</option>)}
        </select>

        <div className="flex-none text-small tabular-nums text-fg-subtle">{category.line_items.length} items</div>

        <div className="text-right">
          <div className="text-body font-semibold tabular-nums text-fg">{formatMoney(subtotal, budget.currency)}</div>
          <div className="text-small text-fg-subtle">Subtotal</div>
        </div>

        <div className="flex flex-none items-center gap-1">
          <IconButton
            size="sm"
            label={`Move ${category.name} up`}
            icon={<ChevronDown className="rotate-180" />}
            onClick={() => onMove('up')}
            disabled={isFirst}
          />
          <IconButton
            size="sm"
            label={`Move ${category.name} down`}
            icon={<ChevronDown />}
            onClick={() => onMove('down')}
            disabled={isLast}
          />
          <IconButton
            size="sm"
            variant={confirmDelete ? 'danger' : 'ghost'}
            label={confirmDelete ? `Confirm delete of ${category.name} — its line items go with it` : `Delete ${category.name}`}
            icon={<Trash2 />}
            onClick={handleDeleteClick}
          />
        </div>
      </div>

      {expanded && (
        <>
          <div className="overflow-x-auto" id={`cat-${category.id}`}>
            <table className="w-full min-w-[940px] text-body">
              <thead>
                <tr className="bg-surface-muted text-eyebrow uppercase text-fg-subtle">
                  <th className="px-5 py-2 text-left font-semibold">Line item</th>
                  <th className="px-2 py-2 text-right font-semibold" style={{ width: 80 }}>Qty</th>
                  <th className="px-2 py-2 text-right font-semibold" style={{ width: 110 }}>Unit cost</th>
                  <th className="px-2 py-2 text-left font-semibold" style={{ width: 110 }}>Unit</th>
                  <th className="px-2 py-2 text-right font-semibold" style={{ width: 130 }}>Total</th>
                  <th className="px-2 py-2 text-left font-semibold" style={{ width: 140 }}>Status</th>
                  <th className="px-2 py-2" style={{ width: 112 }} />
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
                    <td colSpan={7} className="px-5 py-6 text-center text-body text-fg-subtle">
                      No line items — this category will still appear in the Word document with a zero subtotal.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>

          <div className="flex flex-wrap items-center justify-between gap-3 border-t border-line px-5 py-3">
            <Button
              variant="secondary"
              size="sm"
              leftIcon={<Plus aria-hidden />}
              onClick={handleAddLine}
              disabled={addingLine}
            >
              {addingLine ? 'Adding…' : 'Add line item'}
            </Button>
            {nonCostedCount > 0 && (
              <p className="text-small text-fg-subtle">
                {nonCostedCount} of {category.line_items.length} rows carry no cost (see Status) and are excluded from
                this subtotal — they still appear in the Word document.
              </p>
            )}
          </div>
        </>
      )}
    </Card>
  )
}
