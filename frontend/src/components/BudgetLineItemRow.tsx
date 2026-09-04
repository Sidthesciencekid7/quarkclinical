import { useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import { formatMoney, formatQty, parseMoneyInput } from './budgetFormat'
import { BUDGET_LINE_STATUS_LABELS } from '../types'
import type { BudgetDerivation, BudgetLineItem, BudgetLineItemUpdatePayload, BudgetLineSource, BudgetLineStatus } from '../types'

interface Props {
  budgetId: string
  item: BudgetLineItem
  currency: string
  /** The budget's provenance record (for the badge tooltip + quantity caption
   *  on protocol_derived/rate_card rows). Null for a legacy hand-built budget
   *  with no derivation at all. */
  derivation: BudgetDerivation | null
  isFirst: boolean
  isLast: boolean
  /** True for a freshly-created row — focuses its label input on mount. */
  autoFocusLabel?: boolean
  /** Only badge a hand-added "manual" row when the budget has a derivation —
   *  in a legacy hand-built budget every row would be badged, which is noise. */
  showManualBadge: boolean
  onChanged: (li: BudgetLineItem) => void
  onDeleted: () => void
  onMove: (dir: 'up' | 'down') => void
  /** Fired after any successful write so the editor refreshes the rollup. */
  onDirty: () => void
  onError: (msg: string) => void
}

const STATUSES: BudgetLineStatus[] = [
  'costed',
  'out_of_scope',
  'not_applicable',
  'not_planned',
  'outsourced',
  'actuals_only',
]

// Deliberately quiet, non-alarming palette — none of these are errors.
// outsourced/actuals_only get their own hues because they're the two that
// mean "real money exists, it just isn't in this cell".
const LINE_STATUS_CHIP: Record<BudgetLineStatus, string> = {
  costed: '',
  out_of_scope: 'bg-slate-100 text-slate-500 border-slate-200',
  not_applicable: 'bg-slate-100 text-slate-500 border-slate-200',
  not_planned: 'bg-slate-100 text-slate-500 border-slate-200',
  outsourced: 'bg-violet-50 text-violet-700 border-violet-200',
  // Same family as `outsourced` — both mean "real money, billed elsewhere".
  // Amber is reserved solely for "a human overrode the estimate" now, so
  // actuals_only can no longer share that hue.
  actuals_only: 'bg-violet-50 text-violet-700 border-violet-200',
}

const STATUS_HELP: Record<BudgetLineStatus, string> = {
  costed: '',
  out_of_scope: 'Excluded from this study as designed — not part of its scope.',
  not_applicable: 'Does not apply to this study.',
  not_planned: 'Not currently planned for this budget.',
  outsourced: 'Handled by a third party and invoiced outside this budget.',
  actuals_only: 'Billed at actual cost when incurred — no estimate is carried in this budget.',
}

// Three-way provenance badge. `rate_card` is the overwhelming majority of
// rows in an auto-populated budget, so it gets the QUIETEST treatment — a
// grey badge and a plain grey rail, no background tint. A 60-row wall of
// amber/cyan would read as 60 problems instead of the normal, expected
// content it is. Cyan is reserved for the rarer, better-provenanced
// protocol rows; amber is reserved solely for a human-edited price/qty.
const SOURCE_BADGE: Record<BudgetLineSource, { label: string; cls: string; title: string } | null> = {
  rate_card: {
    label: 'Estimated',
    cls: 'text-slate-500 bg-slate-100 border-slate-200',
    title: 'Quantity and unit cost are calibrated approximations from our CRO rate card. Edit either one.',
  },
  protocol_derived: {
    label: 'From protocol',
    cls: 'text-cyan-700 bg-cyan-50 border-cyan-100',
    title: 'Quantity comes from your linked protocol’s real schedule.',
  },
  manual: {
    label: 'Yours',
    cls: 'text-slate-600 bg-white border-slate-200',
    title: 'You created this row.',
  },
}
const SOURCE_RAIL: Record<BudgetLineSource, string> = {
  rate_card: 'border-slate-200',
  protocol_derived: 'border-cyan-300 bg-cyan-50/30',
  manual: 'border-transparent',
}

function Spinner({ size = 13 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none" className="animate-spin">
      <circle cx="8" cy="8" r="6" stroke="#e0f2fe" strokeWidth="2.5" />
      <path d="M8 2a6 6 0 016 6" stroke="#0891b2" strokeWidth="2.5" strokeLinecap="round" />
    </svg>
  )
}
function ChevronUpIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 20 20" fill="none">
      <path d="M5 12l5-5 5 5" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}
function ChevronDownIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 20 20" fill="none">
      <path d="M5 8l5 5 5-5" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}
function TrashIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 20 20" fill="none">
      <path d="M4 6h12M8 6V4.5A1.5 1.5 0 019.5 3h1A1.5 1.5 0 0112 4.5V6m-6.5 0l.6 10.2a1.5 1.5 0 001.5 1.4h4.8a1.5 1.5 0 001.5-1.4L14.5 6" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}
function RefreshIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 20 20" fill="none">
      <path d="M4 10a6 6 0 0110-4.24M16 10a6 6 0 01-10 4.24" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
      <path d="M14 3v3.5h-3.5M6 17v-3.5h3.5" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}

/** Builds the "176 subjects × 14 PK timepoints"-style caption for a derived
 *  row. Namespaced keys (e.g. "dm.edc_license") are server-defined and never
 *  match a closed set, so the server's own `basis` string — rendered
 *  verbatim — is preferred; the three legacy closed-union keys are kept as a
 *  fallback for an old protocol-only derivation that predates `basis`. */
function provenanceText(derivation: BudgetDerivation, key: string, fallbackQty: number, fallbackUnit: string | null, basis?: string): string {
  if (basis) return basis
  if (key === 'pk_draws' && derivation.n_enrolled != null && derivation.n_pk_timepoints != null) {
    return `${derivation.n_enrolled} subjects × ${derivation.n_pk_timepoints} PK timepoints`
  }
  if (key === 'ada_draws' && derivation.n_enrolled != null && derivation.n_ada_timepoints != null) {
    return `${derivation.n_enrolled} subjects × ${derivation.n_ada_timepoints} ADA timepoints`
  }
  if (key === 'subject_visits' && derivation.n_enrolled != null && derivation.n_visit_days != null) {
    return `${derivation.n_enrolled} subjects × ${derivation.n_visit_days} visit days`
  }
  return `${formatQty(fallbackQty)}${fallbackUnit ? ` ${fallbackUnit}` : ''}`
}

export default function BudgetLineItemRow({
  budgetId, item, currency, derivation, isFirst, isLast, autoFocusLabel, showManualBadge,
  onChanged, onDeleted, onMove, onDirty, onError,
}: Props) {
  const [label, setLabel] = useState(item.label)
  const [quantity, setQuantity] = useState(item.quantity != null ? String(item.quantity) : '')
  const [unitCost, setUnitCost] = useState(item.unit_cost != null ? String(item.unit_cost) : '')
  const [unitDesc, setUnitDesc] = useState(item.unit_description ?? '')
  const [total, setTotal] = useState(item.total != null ? String(item.total) : '')
  const [saving, setSaving] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const labelRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    if (autoFocusLabel) labelRef.current?.focus()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  function resetDraft(src: BudgetLineItem) {
    setLabel(src.label)
    setQuantity(src.quantity != null ? String(src.quantity) : '')
    setUnitCost(src.unit_cost != null ? String(src.unit_cost) : '')
    setUnitDesc(src.unit_description ?? '')
    setTotal(src.total != null ? String(src.total) : '')
  }

  /** Every write in this row goes through here so failure handling (revert +
   *  surface the message in the editor's shared banner) is uniform, and the
   *  rollup refresh (onDirty) always follows a successful write. */
  async function save(patch: BudgetLineItemUpdatePayload) {
    setSaving(true)
    try {
      const updated = await api.updateBudgetLineItem(budgetId, item.id, patch)
      onChanged(updated)
      onDirty()
    } catch (e) {
      onError(e instanceof Error ? e.message : 'Failed to save line item')
      resetDraft(item)
    } finally {
      setSaving(false)
    }
  }

  function commitLabel() {
    const trimmed = label.trim()
    if (!trimmed) { setLabel(item.label); return }
    if (trimmed === item.label) return
    save({ label: trimmed })
  }

  // THE TOTAL RULE: omit `total` entirely and send both quantity and
  // unit_cost -> the server computes total = quantity * unit_cost. Blurring
  // EITHER field re-sends both merged values with `total` absent.
  function commitQuantityOrUnitCost() {
    const q = parseMoneyInput(quantity)
    const u = parseMoneyInput(unitCost)
    if (q === item.quantity && u === item.unit_cost) return
    save({ quantity: q, unit_cost: u })
  }

  // Sending `total` (a number or null) makes the server store it verbatim and
  // stop recomputing — for lump-sum rows with no unit arithmetic.
  function commitTotal() {
    const t = parseMoneyInput(total)
    if (t === item.total) return
    save({ total: t })
  }

  function commitUnitDesc() {
    const trimmed = unitDesc.trim()
    const next = trimmed === '' ? null : trimmed
    if (next === item.unit_description) return
    save({ unit_description: next })
  }

  // Changing status away from `costed` must NOT clear quantity/unit_cost/
  // total — send only `{ status }`, so the server keeps the stored numbers
  // and simply stops summing them. Switching back to `costed` restores the
  // money display from those untouched values automatically.
  function handleStatusChange(status: BudgetLineStatus) {
    if (status === item.status) return
    save({ status })
  }

  // The contract-exact way back to a computed total: re-send the same
  // quantity/unit_cost with `total` omitted.
  function handleRecalculate() {
    save({ quantity: item.quantity, unit_cost: item.unit_cost })
  }

  function handleDeleteClick() {
    if (!confirmDelete) {
      setConfirmDelete(true)
      window.setTimeout(() => setConfirmDelete(false), 4000)
      return
    }
    setConfirmDelete(false)
    setSaving(true)
    api.deleteBudgetLineItem(budgetId, item.id)
      .then(() => { onDeleted(); onDirty() })
      .catch(e => { onError(e instanceof Error ? e.message : 'Failed to delete line item'); setSaving(false) })
  }

  const derivationLine = item.derivation_key && derivation
    ? derivation.lines.find(l => l.derivation_key === item.derivation_key) ?? null
    : null
  const isDerived = item.source === 'protocol_derived'

  // "Edited" detection is rewired per the finalized contract: PATCH refuses
  // to change source/derivation_key, so a corrected rate_card row keeps its
  // "Estimated" badge forever — the only reliable signal that a human
  // overrode the estimate is the live value diverging from what the engine
  // originally assigned, not the row's `source`.
  const autoPrice = item.derivation_key && derivation?.auto_priced
    ? derivation.auto_priced[item.derivation_key] ?? null
    : null
  const isEditedPrice = autoPrice != null && item.unit_cost != null && Math.abs(item.unit_cost - autoPrice) > 0.005
  const isEditedQty = derivationLine != null && item.quantity !== derivationLine.quantity
  const isEdited = isEditedPrice || isEditedQty

  const badge = SOURCE_BADGE[item.source]
  const showBadge = item.source !== 'manual' || showManualBadge

  const isNonCosted = item.status !== 'costed'

  const computedTotal = item.quantity != null && item.unit_cost != null ? item.quantity * item.unit_cost : null
  const totalMatchesComputed = computedTotal != null && item.total != null && Math.abs(item.total - computedTotal) < 0.005
  const showRecalc = computedTotal != null && !totalMatchesComputed

  const fieldMuted = isNonCosted ? 'text-slate-400 bg-slate-50/60' : ''
  const quantityWeight = isDerived ? 'font-medium text-slate-700' : 'font-semibold text-slate-900'

  const editedTitle = isEditedPrice
    ? `Estimated at ${formatMoney(autoPrice, currency)} — you set ${formatMoney(item.unit_cost, currency)}`
    : isEditedQty && derivationLine
      ? `Estimated at ${formatQty(derivationLine.quantity)} — you set ${formatQty(item.quantity)}`
      : undefined

  return (
    <tr className={`${isFirst ? '' : 'border-t border-slate-100'} border-l-2 ${SOURCE_RAIL[item.source]}`}>
      <td className="px-5 py-2.5 align-top">
        <div className="flex items-center gap-1.5 flex-wrap">
          <input
            ref={labelRef}
            value={label}
            onChange={e => setLabel(e.target.value)}
            onBlur={commitLabel}
            onKeyDown={e => {
              if (e.key === 'Enter') (e.target as HTMLInputElement).blur()
              if (e.key === 'Escape') setLabel(item.label)
            }}
            className="font-semibold text-slate-900 bg-transparent border border-transparent hover:border-slate-200 focus:border-cyan-400 focus:ring-2 focus:ring-cyan-500/40 rounded-lg px-2 py-1 min-w-[160px] w-full max-w-[320px]"
          />
          {showBadge && badge && (
            <span
              className={`text-[10px] font-bold px-1.5 py-0.5 rounded-full whitespace-nowrap border ${badge.cls}`}
              title={badge.title}
            >
              {badge.label}
            </span>
          )}
          {isEdited && (
            <span className="text-[10px] font-bold text-amber-700 bg-amber-50 border border-amber-200 px-1.5 py-0.5 rounded-full whitespace-nowrap" title={editedTitle}>
              Edited
            </span>
          )}
        </div>
        {derivationLine && (
          <p className="text-[11px] text-slate-500 mt-0.5" title={editedTitle ?? undefined}>
            {editedTitle ?? provenanceText(derivation!, derivationLine.derivation_key, derivationLine.quantity, derivationLine.unit_description, derivationLine.basis)}
          </p>
        )}
      </td>

      <td className="px-2 py-2.5 align-top text-right">
        <input
          value={quantity}
          onChange={e => setQuantity(e.target.value)}
          onBlur={commitQuantityOrUnitCost}
          onKeyDown={e => {
            if (e.key === 'Enter') (e.target as HTMLInputElement).blur()
            if (e.key === 'Escape') setQuantity(item.quantity != null ? String(item.quantity) : '')
          }}
          placeholder="—"
          className={`w-full text-right tabular-nums border border-transparent hover:border-slate-200 focus:border-cyan-400 focus:ring-2 focus:ring-cyan-500/40 rounded-lg px-2 py-1 ${quantityWeight} ${fieldMuted}`}
        />
      </td>

      <td className="px-2 py-2.5 align-top text-right">
        <input
          value={unitCost}
          onChange={e => setUnitCost(e.target.value)}
          onBlur={commitQuantityOrUnitCost}
          onKeyDown={e => {
            if (e.key === 'Enter') (e.target as HTMLInputElement).blur()
            if (e.key === 'Escape') setUnitCost(item.unit_cost != null ? String(item.unit_cost) : '')
          }}
          placeholder="—"
          className={`w-full text-right tabular-nums font-semibold text-slate-900 border border-transparent hover:border-slate-200 focus:border-cyan-400 focus:ring-2 focus:ring-cyan-500/40 rounded-lg px-2 py-1 ${fieldMuted}`}
        />
      </td>

      <td className="px-2 py-2.5 align-top">
        <input
          value={unitDesc}
          onChange={e => setUnitDesc(e.target.value)}
          onBlur={commitUnitDesc}
          onKeyDown={e => {
            if (e.key === 'Enter') (e.target as HTMLInputElement).blur()
            if (e.key === 'Escape') setUnitDesc(item.unit_description ?? '')
          }}
          placeholder="—"
          className={`w-full border border-transparent hover:border-slate-200 focus:border-cyan-400 focus:ring-2 focus:ring-cyan-500/40 rounded-lg px-2 py-1 text-slate-600 ${fieldMuted}`}
        />
      </td>

      <td className="px-2 py-2.5 align-top text-right">
        {isNonCosted ? (
          <span
            aria-label={`No cost carried — ${BUDGET_LINE_STATUS_LABELS[item.status]}`}
            title={STATUS_HELP[item.status]}
            className={`tag whitespace-nowrap ${LINE_STATUS_CHIP[item.status]}`}
          >
            {BUDGET_LINE_STATUS_LABELS[item.status]}
          </span>
        ) : (
          <div>
            <div className="flex items-center justify-end gap-1">
              {showRecalc && (
                <button
                  onClick={handleRecalculate}
                  title="Recalculate from quantity × unit cost"
                  aria-label="Recalculate total from quantity times unit cost"
                  className="text-slate-400 hover:text-cyan-600 cursor-pointer focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-400/60 rounded"
                >
                  <RefreshIcon />
                </button>
              )}
              <input
                value={total}
                onChange={e => setTotal(e.target.value)}
                onBlur={commitTotal}
                onKeyDown={e => {
                  if (e.key === 'Enter') (e.target as HTMLInputElement).blur()
                  if (e.key === 'Escape') setTotal(item.total != null ? String(item.total) : '')
                }}
                placeholder="—"
                className="w-full text-right tabular-nums font-semibold text-slate-900 border border-transparent hover:border-slate-200 focus:border-cyan-400 focus:ring-2 focus:ring-cyan-500/40 rounded-lg px-2 py-1"
              />
            </div>
            {totalMatchesComputed && item.quantity != null && item.unit_cost != null && (
              <div className="text-[10px] text-slate-400 text-right pr-2">
                = {formatQty(item.quantity)} × {formatMoney(item.unit_cost, currency)}
              </div>
            )}
          </div>
        )}
      </td>

      <td className="px-2 py-2.5 align-top">
        <select
          value={item.status}
          onChange={e => handleStatusChange(e.target.value as BudgetLineStatus)}
          className="w-full text-xs font-medium text-slate-600 bg-slate-50 border border-slate-200 rounded-lg px-2 py-1.5 cursor-pointer focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-400/60"
        >
          {STATUSES.map(s => <option key={s} value={s}>{BUDGET_LINE_STATUS_LABELS[s]}</option>)}
        </select>
      </td>

      <td className="px-2 py-2.5 align-top">
        <div className="flex items-center gap-0.5 justify-end">
          {saving ? (
            <Spinner />
          ) : (
            <>
              <button
                onClick={() => onMove('up')}
                disabled={isFirst}
                aria-label={`Move ${item.label} up`}
                className="p-1 rounded text-slate-400 hover:text-slate-700 hover:bg-slate-100 disabled:opacity-30 disabled:cursor-not-allowed cursor-pointer focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-400/60"
              >
                <ChevronUpIcon />
              </button>
              <button
                onClick={() => onMove('down')}
                disabled={isLast}
                aria-label={`Move ${item.label} down`}
                className="p-1 rounded text-slate-400 hover:text-slate-700 hover:bg-slate-100 disabled:opacity-30 disabled:cursor-not-allowed cursor-pointer focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-400/60"
              >
                <ChevronDownIcon />
              </button>
              <button
                onClick={handleDeleteClick}
                aria-label={confirmDelete ? `Confirm delete of ${item.label}` : `Delete ${item.label}`}
                className={`p-1 rounded cursor-pointer focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-400/60 ${
                  confirmDelete ? 'text-red-600 bg-red-50' : 'text-slate-400 hover:text-red-600 hover:bg-red-50'
                }`}
              >
                <TrashIcon />
              </button>
            </>
          )}
        </div>
      </td>
    </tr>
  )
}
