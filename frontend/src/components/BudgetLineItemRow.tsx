import { useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import { formatMoney, formatQty, parseMoneyInput } from './budgetFormat'
import { BUDGET_LINE_STATUS_LABELS } from '../types'
import type { BudgetDerivation, BudgetLineItem, BudgetLineItemUpdatePayload, BudgetLineSource, BudgetLineStatus, BudgetProvenanceTier, BudgetSourceRecord } from '../types'
import { Badge, ChevronDown, IconButton, Input, RefreshCw, Select, Spinner, Trash2, X } from '../ui'
import type { BadgeVariant } from '../ui'

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

// Deliberately quiet, non-alarming tones — none of these are errors.
// outsourced/actuals_only get their own tone because they're the two that
// mean "real money exists, it just isn't in this cell".
const LINE_STATUS_VARIANT: Record<BudgetLineStatus, BadgeVariant> = {
  costed: 'neutral',
  out_of_scope: 'neutral',
  not_applicable: 'neutral',
  not_planned: 'neutral',
  outsourced: 'info',
  // Same family as `outsourced` — both mean "real money, billed elsewhere".
  // Warning is reserved solely for "a human overrode the estimate".
  actuals_only: 'info',
}

const STATUS_HELP: Record<BudgetLineStatus, string> = {
  costed: '',
  out_of_scope: 'Excluded from this study as designed — not part of its scope.',
  not_applicable: 'Does not apply to this study.',
  not_planned: 'Not currently planned for this budget.',
  outsourced: 'Handled by a third party and invoiced outside this budget.',
  actuals_only: 'Billed at actual cost when incurred — no estimate is carried in this budget.',
}

// The same badge classes, for the one badge that is a real <button> (the
// "Sourced" badge that opens the citation popover). Full literal strings so
// Tailwind keeps them.
const BADGE_BUTTON_CLASS: Record<BadgeVariant, string> = {
  neutral: 'badge badge-neutral',
  solid: 'badge badge-solid',
  success: 'badge badge-success',
  warning: 'badge badge-warning',
  danger: 'badge badge-danger',
  info: 'badge badge-info',
}

// Four-way provenance badge. `rate_card` is the majority of rows in an
// auto-populated healthy-volunteer budget, so it stays the QUIETEST
// treatment — a neutral badge and a plain neutral rail. A 60-row wall of
// amber/cyan would read as 60 problems instead of the normal, expected
// content it is. Info is reserved for the rarer, better-provenanced protocol
// rows; warning is reserved solely for a human-edited price/qty.
// `researched` inverts that hierarchy — on a patient budget "this number has
// a citation" is the signal worth finding, so it is the LOUDEST of the four.
const SOURCE_BADGE: Record<BudgetLineSource, { label: string; variant: BadgeVariant; title: string } | null> = {
  rate_card: {
    label: 'Estimated',
    variant: 'neutral',
    title: 'Quantity and unit cost are calibrated approximations from our CRO rate card. Edit either one.',
  },
  protocol_derived: {
    label: 'From protocol',
    variant: 'info',
    title: 'Quantity comes from your linked protocol’s real schedule.',
  },
  manual: {
    label: 'Yours',
    variant: 'neutral',
    title: 'You created this row.',
  },
  researched: {
    label: 'Sourced',
    variant: 'success',
    title: 'This unit cost is a real published rate. Click to see the citation.',
  },
}
const SOURCE_RAIL: Record<BudgetLineSource, string> = {
  rate_card: 'border-line',
  protocol_derived: 'border-[color:var(--info-border)]',
  manual: 'border-transparent',
  researched: 'border-[color:var(--ok-border)]',
}

/** Within `researched`, the provenance TIER further modulates the badge so a
 *  `derived` triangulation is never passed off as a `published_rate`
 *  benchmark, a `bid_calibrated` rate reads as one vendor's price rather than
 *  a market rate, and `estimate` (no citation exists at all) falls back to
 *  the same quiet neutral as a plain rate-card row. */
const RESEARCHED_TIER_BADGE: Record<BudgetProvenanceTier, { label: string; variant: BadgeVariant; title: string }> = {
  published_rate: {
    label: 'Sourced',
    variant: 'success',
    title: 'A real, published rate. Click to see the citation.',
  },
  derived: {
    label: 'Derived',
    variant: 'warning',
    title: 'Triangulated by us from two or more published figures — not itself a published benchmark. Click to see the sources.',
  },
  bid_calibrated: {
    label: 'CRO bid',
    variant: 'info',
    title: 'Taken from a real CRO bid for a comparable study — one vendor’s price, not a market benchmark. Click for details.',
  },
  estimate: {
    label: 'Estimated',
    variant: 'neutral',
    title: 'No public figure exists for this item — a rate-card approximation.',
  },
}

/** Plain-English meaning of each provenance tier, shown at the top of the
 *  source popover. Mirrors the backend's PROVENANCE_TIERS copy verbatim. */
const PROVENANCE_TIER_COPY: Record<BudgetProvenanceTier, string> = {
  published_rate: 'Published rate — read directly from an official fee schedule, manufacturer price list, government schedule or peer-reviewed benchmark.',
  derived: 'Derived — triangulated by us from two or more published figures. Not itself a published benchmark.',
  bid_calibrated: 'Bid-calibrated — a unit rate taken from a real CRO bid for a comparable study. A single vendor’s price, not a market benchmark.',
  estimate: 'Estimate — a rate-card approximation. No public figure exists for this item; treat it as an assumption to be replaced by a quote.',
}

/** Short label for a geography profile, used only in a tooltip naming what
 *  the geography factor scales. */
const GEOGRAPHY_LABEL: Record<string, string> = {
  us_only: 'United States only',
  eu_west: 'Western Europe',
  eu_east: 'Eastern Europe',
  latam: 'Latin America',
  apac: 'Asia-Pacific',
  china: 'China',
  india: 'India',
  global_emerging_mixed: 'Global — emerging markets mix',
  global_developed_mixed: 'Global — developed markets mix',
}

/** The citation popover behind the "Sourced" / "Derived" / "CRO bid" badge.
 *  Everything it needs is already in the budget payload — no fetch. */
function SourcePopover({ source, onClose }: { source: BudgetSourceRecord; onClose: () => void }) {
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    function handleOutside(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose()
    }
    document.addEventListener('mousedown', handleOutside)
    return () => document.removeEventListener('mousedown', handleOutside)
  }, [onClose])

  return (
    <div
      ref={ref}
      role="dialog"
      aria-label="Source citation"
      className="absolute left-0 top-full z-50 mt-1.5 w-80 max-w-[85vw] cursor-default rounded-card border border-line bg-surface-pop p-4 text-left text-small font-normal normal-case text-fg shadow-pop"
    >
      <div className="mb-2 flex items-start justify-between gap-2">
        <Badge className="capitalize">{source.tier.replace(/_/g, ' ')}</Badge>
        <IconButton size="sm" label="Close" icon={<X />} onClick={onClose} className="-mr-1 -mt-1" />
      </div>
      <p className="mb-2.5 text-fg-subtle">{PROVENANCE_TIER_COPY[source.tier]}</p>
      <p className="mb-1.5 text-body font-semibold text-fg">{source.figure}</p>
      <p className="mb-2 text-fg-muted">{source.citation}</p>
      {source.url && (
        <a
          href={source.url}
          target="_blank"
          rel="noopener noreferrer"
          className="mb-1 block break-all font-medium text-[color:var(--link)] underline-offset-2 hover:underline"
        >
          {source.url}
        </a>
      )}
      <p className="text-fg-subtle">Accessed {source.accessed}</p>
      {source.caveat && (
        <p className="mt-2 rounded-lg border border-[color:var(--warn-border)] bg-[var(--warn-bg)] px-2.5 py-2 text-[color:var(--warn-fg)]">
          {source.caveat}
        </p>
      )}
    </div>
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

// Borderless-until-hover cell inputs: a dense table of always-boxed fields
// reads as a spreadsheet, so the box appears on hover and focus.
const CELL_INPUT = 'h-9 border-transparent px-2 py-1 hover:border-line-strong'

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
  const [showSourcePopover, setShowSourcePopover] = useState(false)
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

  const provenanceTier = derivationLine?.provenance_tier ?? null
  const badge = item.source === 'researched' && provenanceTier
    ? RESEARCHED_TIER_BADGE[provenanceTier]
    : SOURCE_BADGE[item.source]
  const showBadge = item.source !== 'manual' || showManualBadge
  const sourceRecord = item.source === 'researched' && derivationLine?.source_id && derivation?.sources
    ? derivation.sources[derivationLine.source_id] ?? null
    : null
  const isGeoScaled = derivationLine?.unit_cost_published != null
    && derivationLine.geography_factor != null
    && derivationLine.unit_cost_published !== item.unit_cost

  const isNonCosted = item.status !== 'costed'

  const computedTotal = item.quantity != null && item.unit_cost != null ? item.quantity * item.unit_cost : null
  const totalMatchesComputed = computedTotal != null && item.total != null && Math.abs(item.total - computedTotal) < 0.005
  const showRecalc = computedTotal != null && !totalMatchesComputed

  // Non-costed rows keep their stored numbers but read as inactive.
  const fieldMuted = isNonCosted ? 'bg-surface-muted text-fg-subtle' : 'bg-transparent'
  const quantityWeight = isDerived ? 'font-medium' : 'font-semibold'

  const editedTitle = isEditedPrice
    ? `Estimated at ${formatMoney(autoPrice, currency)} — you set ${formatMoney(item.unit_cost, currency)}`
    : isEditedQty && derivationLine
      ? `Estimated at ${formatQty(derivationLine.quantity)} — you set ${formatQty(item.quantity)}`
      : undefined

  return (
    <tr className={`${isFirst ? '' : 'border-t border-line'} border-l-2 ${SOURCE_RAIL[item.source]}`}>
      <td className="px-5 py-2.5 align-top">
        <div className="flex flex-wrap items-center gap-1.5">
          <div className="w-full min-w-[160px] max-w-[320px]">
            <Input
              ref={labelRef}
              value={label}
              onChange={e => setLabel(e.target.value)}
              onBlur={commitLabel}
              onKeyDown={e => {
                if (e.key === 'Enter') (e.target as HTMLInputElement).blur()
                if (e.key === 'Escape') setLabel(item.label)
              }}
              aria-label="Line item label"
              className={`${CELL_INPUT} bg-transparent font-semibold`}
            />
          </div>
          {showBadge && badge && (
            sourceRecord ? (
              <span className="relative inline-block">
                <button
                  type="button"
                  onClick={() => setShowSourcePopover(s => !s)}
                  className={`${BADGE_BUTTON_CLASS[badge.variant]} cursor-pointer transition-opacity duration-fast hover:opacity-80`}
                  title={badge.title}
                  aria-expanded={showSourcePopover}
                >
                  {badge.label}
                </button>
                {showSourcePopover && (
                  <SourcePopover source={sourceRecord} onClose={() => setShowSourcePopover(false)} />
                )}
              </span>
            ) : (
              <Badge variant={badge.variant} title={badge.title}>
                {badge.label}
              </Badge>
            )
          )}
          {isEdited && (
            <Badge variant="warning" title={editedTitle}>
              Edited
            </Badge>
          )}
        </div>
        {derivationLine && (
          <p className="mt-0.5 px-2 text-xs leading-4 text-fg-subtle" title={editedTitle ?? undefined}>
            {editedTitle ?? provenanceText(derivation!, derivationLine.derivation_key, derivationLine.quantity, derivationLine.unit_description, derivationLine.basis)}
          </p>
        )}
      </td>

      <td className="px-2 py-2.5 align-top text-right">
        <Input
          value={quantity}
          onChange={e => setQuantity(e.target.value)}
          onBlur={commitQuantityOrUnitCost}
          onKeyDown={e => {
            if (e.key === 'Enter') (e.target as HTMLInputElement).blur()
            if (e.key === 'Escape') setQuantity(item.quantity != null ? String(item.quantity) : '')
          }}
          placeholder="—"
          aria-label={`Quantity for ${item.label}`}
          className={`${CELL_INPUT} text-right tabular-nums ${quantityWeight} ${fieldMuted}`}
        />
      </td>

      <td className="px-2 py-2.5 align-top text-right">
        <Input
          value={unitCost}
          onChange={e => setUnitCost(e.target.value)}
          onBlur={commitQuantityOrUnitCost}
          onKeyDown={e => {
            if (e.key === 'Enter') (e.target as HTMLInputElement).blur()
            if (e.key === 'Escape') setUnitCost(item.unit_cost != null ? String(item.unit_cost) : '')
          }}
          placeholder="—"
          aria-label={`Unit cost for ${item.label}`}
          className={`${CELL_INPUT} text-right font-semibold tabular-nums ${fieldMuted}`}
        />
        {isGeoScaled && (
          <div
            className="pr-2 text-right text-xs leading-4 tabular-nums text-fg-subtle"
            title={`Scaled for the ${GEOGRAPHY_LABEL[derivation?.geography_profile ?? ''] ?? 'selected'} geography profile.`}
          >
            ({derivationLine!.unit_cost_published!.toLocaleString()} × {derivationLine!.geography_factor} geo)
          </div>
        )}
      </td>

      <td className="px-2 py-2.5 align-top">
        <Input
          value={unitDesc}
          onChange={e => setUnitDesc(e.target.value)}
          onBlur={commitUnitDesc}
          onKeyDown={e => {
            if (e.key === 'Enter') (e.target as HTMLInputElement).blur()
            if (e.key === 'Escape') setUnitDesc(item.unit_description ?? '')
          }}
          placeholder="—"
          aria-label={`Unit for ${item.label}`}
          className={`${CELL_INPUT} text-fg-muted ${fieldMuted}`}
        />
      </td>

      <td className="px-2 py-2.5 align-top text-right">
        {isNonCosted ? (
          <Badge
            variant={LINE_STATUS_VARIANT[item.status]}
            aria-label={`No cost carried — ${BUDGET_LINE_STATUS_LABELS[item.status]}`}
            title={STATUS_HELP[item.status]}
            className="mt-1.5"
          >
            {BUDGET_LINE_STATUS_LABELS[item.status]}
          </Badge>
        ) : (
          <div>
            <div className="flex items-center justify-end gap-1">
              {showRecalc && (
                <IconButton
                  size="sm"
                  label="Recalculate total from quantity times unit cost"
                  icon={<RefreshCw />}
                  onClick={handleRecalculate}
                  className="flex-none"
                />
              )}
              <Input
                value={total}
                onChange={e => setTotal(e.target.value)}
                onBlur={commitTotal}
                onKeyDown={e => {
                  if (e.key === 'Enter') (e.target as HTMLInputElement).blur()
                  if (e.key === 'Escape') setTotal(item.total != null ? String(item.total) : '')
                }}
                placeholder="—"
                aria-label={`Total for ${item.label}`}
                className={`${CELL_INPUT} bg-transparent text-right font-semibold tabular-nums`}
              />
            </div>
            {totalMatchesComputed && item.quantity != null && item.unit_cost != null && (
              <div className="pr-2 text-right text-xs leading-4 tabular-nums text-fg-subtle">
                = {formatQty(item.quantity)} × {formatMoney(item.unit_cost, currency)}
              </div>
            )}
          </div>
        )}
      </td>

      <td className="px-2 py-2.5 align-top">
        <Select
          value={item.status}
          onChange={e => handleStatusChange(e.target.value as BudgetLineStatus)}
          aria-label={`Status for ${item.label}`}
          className="h-9 py-1 pl-2 text-small"
        >
          {STATUSES.map(s => <option key={s} value={s}>{BUDGET_LINE_STATUS_LABELS[s]}</option>)}
        </Select>
      </td>

      <td className="px-2 py-2.5 align-top">
        <div className="flex h-9 items-center justify-end gap-0.5">
          {saving ? (
            <Spinner size={16} className="mr-2 text-fg-subtle" />
          ) : (
            <>
              <IconButton
                size="sm"
                label={`Move ${item.label} up`}
                icon={<ChevronDown className="rotate-180" />}
                onClick={() => onMove('up')}
                disabled={isFirst}
              />
              <IconButton
                size="sm"
                label={`Move ${item.label} down`}
                icon={<ChevronDown />}
                onClick={() => onMove('down')}
                disabled={isLast}
              />
              <IconButton
                size="sm"
                variant={confirmDelete ? 'danger' : 'ghost'}
                label={confirmDelete ? `Confirm delete of ${item.label}` : `Delete ${item.label}`}
                icon={<Trash2 />}
                onClick={handleDeleteClick}
              />
            </>
          )}
        </div>
      </td>
    </tr>
  )
}
