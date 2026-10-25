import { useEffect, useState } from 'react'
import { api } from '../api/client'
import { formatMoney } from './budgetFormat'
import { Card, Input, PillNav } from '../ui'
import type { Budget, BudgetDiscountType, BudgetProvenanceTier, BudgetRollup } from '../types'

interface Props {
  budget: Budget
  rollup: BudgetRollup
  /** 'full' (default): the ledger card. 'sticky': the bottom grand-total bar.
   *  Rendered twice by BudgetEditor, sharing this one component so the two
   *  never disagree on the number. */
  variant?: 'full' | 'sticky'
  onBudgetUpdated?: (b: Budget) => void
  onError?: (msg: string) => void
  /** Opens the sources drawer/panel — wired by BudgetEditor. Only used to
   *  make the provenance meter clickable; the meter still renders fine
   *  without it. */
  onOpenSources?: () => void
}

// Same four tiers as the line-item badges (emerald/teal/slate-blue/grey),
// newest-tier-first, for the stacked provenance meter.
const TIER_METER_COLOR: Record<BudgetProvenanceTier, string> = {
  published_rate: '#10b981', // emerald-500
  derived: '#14b8a6', // teal-500
  bid_calibrated: '#6366f1', // indigo-500 ("slate-blue")
  estimate: '#cbd5e1', // slate-300
}

const DISCOUNT_TYPES: { id: BudgetDiscountType; label: string }[] = [
  { id: 'none', label: 'None' },
  { id: 'percent', label: 'Percent' },
  { id: 'flat', label: 'Flat' },
]

function Row({ label, value, bold, valueClassName, className }: {
  label: string
  value: string
  bold?: boolean
  valueClassName?: string
  className?: string
}) {
  return (
    <div className={`flex justify-between gap-6 text-body ${bold ? 'font-semibold' : ''} ${className ?? ''}`}>
      <span className="text-fg-muted">{label}</span>
      <span className={`tabular-nums ${valueClassName ?? 'text-fg'}`}>{value}</span>
    </div>
  )
}

/** The money summary — the ONLY component allowed to render totals. Every
 *  figure comes from the `rollup` prop; nothing here is computed locally. */
export default function BudgetRollupCard({ budget, rollup, variant = 'full', onBudgetUpdated, onError, onOpenSources }: Props) {
  const currency = budget.currency
  const isPatient = budget.subject_profile === 'patient'

  // Hooks run unconditionally regardless of variant, so the two usages of
  // this component (sticky + full) each keep a stable hook order.
  const [discountType, setDiscountType] = useState<BudgetDiscountType>(budget.discount_type)
  const [discountValue, setDiscountValue] = useState(String(budget.discount_value))
  const [discountError, setDiscountError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    setDiscountType(budget.discount_type)
    setDiscountValue(String(budget.discount_value))
  }, [budget.discount_type, budget.discount_value])

  async function handleDiscountSave(nextType: BudgetDiscountType, rawValue: string) {
    setDiscountError(null)
    const value = rawValue.trim() === '' ? 0 : Number(rawValue)
    if (!Number.isFinite(value)) {
      setDiscountError('Enter a valid number.')
      return
    }
    if (nextType === 'percent' && (value < 0 || value > 100)) {
      setDiscountError('A percent discount must be between 0 and 100.')
      return
    }
    if (nextType !== 'none' && value < 0) {
      setDiscountError('Discount cannot be negative.')
      return
    }
    setSaving(true)
    try {
      const updated = await api.updateBudget(budget.id, {
        discount_type: nextType,
        discount_value: nextType === 'none' ? 0 : value,
      })
      onBudgetUpdated?.(updated)
    } catch (e) {
      onError?.(e instanceof Error ? e.message : 'Failed to update discount')
    } finally {
      setSaving(false)
    }
  }

  if (variant === 'sticky') {
    return (
      <div className="sticky bottom-0 z-20 border-t border-line bg-surface px-4 py-3 sm:px-8">
        <div className="mx-auto flex w-full max-w-5xl items-center justify-between gap-4">
          <span className="text-small font-medium text-fg-muted">Grand total</span>
          <span aria-live="polite" className="text-h2 tabular-nums text-fg">
            {formatMoney(rollup.grand_total, currency)}
          </span>
        </div>
      </div>
    )
  }

  return (
    <Card padding="lg">
      <div aria-live="polite" className="ml-auto max-w-sm space-y-2">
        <Row label="Direct subtotal" value={formatMoney(rollup.direct_subtotal, currency)} />

        {rollup.discount_type !== 'none' && (
          <Row
            label={`Discount${rollup.discount_type === 'percent' ? ` (${rollup.discount_value}%)` : ''}`}
            value={`− ${formatMoney(rollup.discount_amount, currency)}`}
            valueClassName="text-[color:var(--bad-fg)]"
          />
        )}

        <Row label="Total direct costs" value={formatMoney(rollup.total_direct_costs, currency)} bold className="border-t border-line pt-2" />
        {rollup.software_total > 0 && (
          <Row label="Software licensing and hosting" value={formatMoney(rollup.software_total, currency)} />
        )}
        <Row label="Subject stipend" value={formatMoney(rollup.subject_stipend_total, currency)} />
        <Row label="Variable" value={formatMoney(rollup.variable_total, currency)} />
        <Row label="Pass-through" value={formatMoney(rollup.pass_through_total, currency)} />
        {rollup.investigator_grants_total > 0 && (
          <Row label="Investigator grants and site fees" value={formatMoney(rollup.investigator_grants_total, currency)} />
        )}

        <div className="flex justify-between gap-6 border-t-2 border-line-strong pt-3">
          <span className="text-h2 text-fg">Grand total</span>
          <span className="text-h2 tabular-nums text-fg">{formatMoney(rollup.grand_total, currency)}</span>
        </div>
      </div>

      {isPatient && budget.derivation?.provenance_summary && (
        <ProvenanceMeter summary={budget.derivation.provenance_summary} onOpenSources={onOpenSources} />
      )}

      <div className="mt-6 space-y-2.5 border-t border-line pt-5">
        <div className="flex flex-wrap items-center gap-3">
          <PillNav
            ariaLabel="Discount type"
            size="sm"
            value={discountType}
            items={DISCOUNT_TYPES.map(dt => ({ id: dt.id, label: dt.label, disabled: saving }))}
            onChange={id => {
              const dt = id as BudgetDiscountType
              setDiscountType(dt)
              handleDiscountSave(dt, dt === 'none' ? '0' : discountValue)
            }}
          />
          {discountType !== 'none' && (
            <div className="w-28">
              <Input
                value={discountValue}
                onChange={e => setDiscountValue(e.target.value)}
                onBlur={() => handleDiscountSave(discountType, discountValue)}
                disabled={saving}
                inputMode="decimal"
                aria-label={discountType === 'percent' ? 'Discount percent' : 'Discount amount'}
                placeholder={discountType === 'percent' ? '0–100' : '0.00'}
                className="tabular-nums"
              />
            </div>
          )}
        </div>
        {discountError && (
          <p role="alert" className="text-small font-medium text-[color:var(--bad-fg)]">
            {discountError}
          </p>
        )}
        <p className="text-small text-fg-subtle">
          {isPatient
            ? 'Applied to professional fees only — investigator grants, software licensing and pass-through are never discounted.'
            : 'Applied to direct costs only.'}
        </p>
        {budget.derivation?.discount_note && (
          <p className="text-small text-fg-subtle">{budget.derivation.discount_note}</p>
        )}
      </div>
    </Card>
  )
}

/** A 3px stacked bar (emerald = published rate, teal = derived, indigo = CRO
 *  bid, grey = estimate) with a one-line summary. Clicking it opens the
 *  sources drawer (BudgetSourcesPanel), when the caller wired one up. */
function ProvenanceMeter({
  summary,
  onOpenSources,
}: {
  summary: NonNullable<Budget['derivation']>['provenance_summary']
  onOpenSources?: () => void
}) {
  if (!summary) return null
  const segments: { tier: BudgetProvenanceTier; value: number }[] = [
    { tier: 'published_rate', value: summary.researched_value },
    { tier: 'derived', value: summary.derived_value },
    { tier: 'bid_calibrated', value: summary.bid_calibrated_value },
    { tier: 'estimate', value: summary.estimate_value },
  ]
  const total = segments.reduce((sum, s) => sum + s.value, 0)
  const pct = (summary.researched_share_of_grand_total * 100).toFixed(1)
  const interactive = !!onOpenSources

  const body = (
    <>
      <div className="flex h-[3px] w-full overflow-hidden rounded-full bg-surface-muted">
        {total > 0 && segments.filter(s => s.value > 0).map(s => (
          <div
            key={s.tier}
            style={{ width: `${(s.value / total) * 100}%`, backgroundColor: TIER_METER_COLOR[s.tier] }}
          />
        ))}
      </div>
      <p className={`mt-2 text-small text-fg-subtle ${interactive ? 'group-hover:text-[color:var(--link)]' : ''}`}>
        {pct}% of this budget rests on published rates we cite.{interactive ? ' See sources →' : ''}
      </p>
    </>
  )

  const wrapperClass = 'mt-5 w-full border-t border-line pt-4 text-left'
  // A real <button> when it opens the sources panel, so Enter/Space and focus
  // come for free; a plain block otherwise.
  return interactive ? (
    <button type="button" onClick={onOpenSources} className={`${wrapperClass} group block cursor-pointer rounded-input`}>
      {body}
    </button>
  ) : (
    <div className={wrapperClass}>{body}</div>
  )
}
