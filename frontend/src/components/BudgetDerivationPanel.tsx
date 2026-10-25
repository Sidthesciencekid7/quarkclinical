import { useState } from 'react'
import { api } from '../api/client'
import BudgetProtocolPicker from './BudgetProtocolPicker'
import { formatMoney, relativeDate } from './budgetFormat'
import { Activity, AlertTriangle, Badge, Button, Card, ChevronDown, LinkIcon } from '../ui'
import type { Budget, BudgetDerivation } from '../types'

interface Props {
  budget: Budget
  /** link-protocol always returns the WHOLE budget — replace state with it. */
  onBudgetUpdated: (b: Budget) => void
  onError: (msg: string) => void
}

type DerivationField = {
  key: 'n_enrolled' | 'n_pk_timepoints' | 'n_ada_timepoints' | 'n_visit_days' | 'design_key' | 'periods'
    | 'modality' | 'complexity' | 'study_type' | 'drug_inn'
  label: string
}

const FIELD_LABELS: DerivationField[] = [
  { key: 'n_enrolled', label: 'Subjects enrolled' },
  { key: 'n_pk_timepoints', label: 'PK timepoints' },
  { key: 'n_ada_timepoints', label: 'ADA timepoints' },
  { key: 'n_visit_days', label: 'Visit days' },
  { key: 'design_key', label: 'Design' },
  { key: 'periods', label: 'Periods' },
  { key: 'modality', label: 'Modality' },
  { key: 'complexity', label: 'Complexity' },
  { key: 'study_type', label: 'Study type' },
  { key: 'drug_inn', label: 'Drug' },
]

const ASSUMPTION_SOURCE_LABEL: Record<string, string> = {
  user: 'yours',
  protocol: 'protocol',
  trial_precedent: 'real trial data',
  default: 'default',
  computed: 'computed',
  inferred_from_modality: 'inferred',
  inferred_from_safety_profile: 'from drug profile',
  phase_scaled: 'scaled',
  kb_default: 'KB default',
}

// A `pass: false` cross-check also appears, verbatim, in `derivation.warnings`.
// Any warning whose text begins with one of these prefixes gets a small
// chart-icon treatment (+ the band it missed) instead of a plain bullet.
const CROSS_CHECK_PREFIXES = [
  'Site monitoring share',
  'Central lab share',
  'RTSM share',
  'Investigator grant share',
]

function humaniseKey(key: string): string {
  const leaf = key.includes('.') ? key.split('.').slice(1).join(' ') : key
  return leaf.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase())
}

const CONDUCT_LABELS: { key: keyof NonNullable<import('../types').BudgetDerivation['conduct']>; label: string }[] = [
  { key: 'target_per_enrolled', label: 'Target / enrolled' },
  { key: 'pk_draw_rate', label: 'PK draw rate' },
  { key: 'ada_draw_rate', label: 'ADA draw rate' },
  { key: 'visit_day_rate', label: 'Visit-day rate' },
  { key: 'screen_failure_ratio', label: 'Screen failure ratio' },
  { key: 'alternate_ratio', label: 'Alternate ratio' },
]

/** Match a warning string to the cross-check it came from, if any, purely by
 *  its known text prefix — `cross_checks` carries the numeric band, but the
 *  warning itself is what's rendered verbatim, so this only decides styling. */
function matchCrossCheck(
  warning: string,
  crossChecks: NonNullable<BudgetDerivation['cross_checks']>,
): NonNullable<BudgetDerivation['cross_checks']>[number] | null {
  const prefix = CROSS_CHECK_PREFIXES.find(p => warning.startsWith(p))
  if (!prefix) return null
  return crossChecks.find(c => c.message?.startsWith(prefix)) ?? crossChecks.find(c => !c.pass) ?? null
}

function WarningList({ warnings, crossChecks }: { warnings: string[]; crossChecks?: NonNullable<BudgetDerivation['cross_checks']> }) {
  if (warnings.length === 0) return null
  return (
    <div className="space-y-2 rounded-card border border-[color:var(--warn-border)] bg-[var(--warn-bg)] px-4 py-3 text-[color:var(--warn-fg)]">
      {warnings.map((w, i) => {
        const check = crossChecks && crossChecks.length > 0 ? matchCrossCheck(w, crossChecks) : null
        return (
          <div key={i} className="flex gap-2.5 text-small">
            {check ? (
              <Activity aria-hidden className="mt-0.5 h-4 w-4 flex-none" />
            ) : (
              <AlertTriangle aria-hidden className="mt-0.5 h-4 w-4 flex-none" />
            )}
            <div className="flex-1">
              <p>{w}</p>
              {check && (
                <p className="mt-0.5 tabular-nums opacity-80">
                  Expected band {(check.band[0] * 100).toFixed(0)}–{(check.band[1] * 100).toFixed(0)}% · actual {(check.value * 100).toFixed(1)}%
                </p>
              )}
            </div>
          </div>
        )
      })}
    </div>
  )
}

/** Disclosure, not an error — amber, never red. Compares this estimate
 *  against real CRO bids for a comparable reference study, patient budgets
 *  only. Rendered as its own bordered callout at the TOP of the panel. */
function BidReconciliation({ data, currency }: { data: NonNullable<BudgetDerivation['bid_reconciliation']>; currency: string }) {
  return (
    <div className="space-y-3 rounded-card border border-[color:var(--warn-border)] bg-[var(--warn-bg)] px-4 py-3.5 text-[color:var(--warn-fg)]">
      <div>
        <h4 className="text-eyebrow uppercase">Reconciled against real CRO bids</h4>
        <p className="mt-0.5 text-small">{data.reference_study}</p>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full text-small">
          <tbody>
            {data.bids.map((b, i) => (
              <tr key={i} className="border-t border-[color:var(--warn-border)] first:border-t-0">
                <td className="py-1.5 pr-3 font-medium">{b.cro}</td>
                <td className="py-1.5 pr-3 text-right font-semibold tabular-nums">
                  {b.grand_total != null ? formatMoney(b.grand_total, currency) : 'n/a'}
                </td>
                <td className="py-1.5 opacity-80">{b.note ?? ''}</td>
              </tr>
            ))}
            <tr className="border-t border-[color:var(--warn-border)]">
              <td className="py-1.5 pr-3 font-semibold">This estimate</td>
              <td className="py-1.5 pr-3 text-right font-semibold tabular-nums">{formatMoney(data.this_estimate, currency)}</td>
              <td className="py-1.5 font-medium tabular-nums">
                +{data.delta_vs_highest_bid_pct.toFixed(1)}% vs. highest bid
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      {data.explained_by.length > 0 && (
        <div>
          <p className="mb-1 text-small font-semibold">What this estimate adds that the bids don&apos;t:</p>
          <ul className="list-inside list-disc space-y-1 text-small">
            {data.explained_by.map((e, i) => <li key={i}>{e}</li>)}
          </ul>
        </div>
      )}

      <p className="border-t border-[color:var(--warn-border)] pt-2 text-small leading-relaxed opacity-90">{data.disclaimer}</p>
    </div>
  )
}

/** The protocol picker plus its Cancel / Link buttons, shared by the slim
 *  legacy prompt and the expanded derivation card. */
function LinkPicker({
  pickerProtocolId,
  setPickerProtocolId,
  linking,
  onCancel,
  onLink,
}: {
  pickerProtocolId: string | null
  setPickerProtocolId: (id: string | null) => void
  linking: boolean
  onCancel: () => void
  onLink: (id: string) => void
}) {
  return (
    <div className="w-full space-y-3">
      <div className="flex items-center justify-between">
        <p className="text-body font-semibold text-fg">Link a protocol</p>
        <Button variant="ghost" size="sm" onClick={onCancel}>
          Cancel
        </Button>
      </div>
      <BudgetProtocolPicker value={pickerProtocolId} onChange={id => setPickerProtocolId(id)} disabled={linking} />
      <div className="flex justify-end">
        <Button
          size="sm"
          onClick={() => pickerProtocolId && onLink(pickerProtocolId)}
          disabled={!pickerProtocolId || linking}
        >
          {linking ? 'Linking…' : 'Link this protocol'}
        </Button>
      </div>
    </div>
  )
}

/** "How we got these numbers." Renders a slim link-prompt only for a legacy/
 *  hand-built budget with no derivation at all; every auto-populated budget
 *  — drug-name-only or protocol-linked — always has a full derivation and
 *  gets the collapsible provenance card. */
export default function BudgetDerivationPanel({ budget, onBudgetUpdated, onError }: Props) {
  const derivation = budget.derivation
  const [expanded, setExpanded] = useState(false)
  const [showAllAssumptions, setShowAllAssumptions] = useState(false)
  const [picking, setPicking] = useState(false)
  const [pickerProtocolId, setPickerProtocolId] = useState<string | null>(null)
  const [linking, setLinking] = useState(false)
  const [confirmUnlink, setConfirmUnlink] = useState(false)

  async function doLink(protocolId: string) {
    setLinking(true)
    try {
      const updated = await api.linkBudgetProtocol(budget.id, protocolId)
      onBudgetUpdated(updated)
      setPicking(false)
    } catch (e) {
      onError(e instanceof Error ? e.message : 'Failed to link protocol')
    } finally {
      setLinking(false)
    }
  }

  async function handleRelink() {
    if (!budget.protocol_id) return
    setLinking(true)
    try {
      const updated = await api.linkBudgetProtocol(budget.id, budget.protocol_id)
      onBudgetUpdated(updated)
    } catch (e) {
      onError(e instanceof Error ? e.message : 'Failed to refresh from protocol')
    } finally {
      setLinking(false)
    }
  }

  function handleUnlinkClick() {
    if (!confirmUnlink) {
      setConfirmUnlink(true)
      window.setTimeout(() => setConfirmUnlink(false), 4000)
      return
    }
    setConfirmUnlink(false)
    setLinking(true)
    api.linkBudgetProtocol(budget.id, null)
      .then(updated => onBudgetUpdated(updated))
      .catch(e => onError(e instanceof Error ? e.message : 'Failed to unlink protocol'))
      .finally(() => setLinking(false))
  }

  // Legacy/hand-built budget with no derivation at all: a slim dashed
  // prompt, with the picker opening in place. A drug-name-only budget
  // always has a full derivation (protocol_linked: false) and skips this.
  if (!derivation) {
    return (
      <div className="rounded-card border border-dashed border-line-strong bg-surface-muted px-5 py-4">
        {!picking ? (
          <div className="flex flex-wrap items-center justify-between gap-3">
            <p className="text-body text-fg-muted">Link a protocol to pull its real cost drivers in.</p>
            <Button variant="secondary" size="sm" leftIcon={<LinkIcon aria-hidden />} onClick={() => setPicking(true)}>
              Link a protocol
            </Button>
          </div>
        ) : (
          <LinkPicker
            pickerProtocolId={pickerProtocolId}
            setPickerProtocolId={setPickerProtocolId}
            linking={linking}
            onCancel={() => setPicking(false)}
            onLink={doLink}
          />
        )}
      </div>
    )
  }

  // Always a full derivation from here on — `skipped` is always false now.
  const assumptions = derivation.assumptions ?? {}
  const assumptionEntries = Object.entries(assumptions)
  const visibleAssumptions = showAllAssumptions || assumptionEntries.length <= 8 ? assumptionEntries : assumptionEntries.slice(0, 8)

  return (
    <Card padding="none" className="overflow-hidden">
      <button
        type="button"
        onClick={() => setExpanded(e => !e)}
        aria-expanded={expanded}
        className="flex w-full cursor-pointer items-center justify-between gap-3 px-5 py-4 text-left"
      >
        <div className="flex flex-wrap items-center gap-2">
          <h3 className="text-h3 text-fg">
            {derivation.protocol_linked ? 'From your protocol' : 'How these figures were built'}
          </h3>
          {derivation.protocol_linked ? (
            <Badge variant="info">From protocol</Badge>
          ) : (
            <>
              <Badge>Estimated</Badge>
              {derivation.rate_card_version && (
                <span className="text-small text-fg-subtle">Rate card {derivation.rate_card_version}</span>
              )}
            </>
          )}
          <span className="text-small text-fg-subtle">Derived {relativeDate(derivation.derived_at)}</span>
        </div>
        <ChevronDown
          aria-hidden
          className={`h-4 w-4 flex-none text-fg-subtle transition-transform duration-fast ${expanded ? 'rotate-180' : ''}`}
        />
      </button>

      {expanded && (
        <div className="space-y-4 px-5 pb-5">
          {derivation.bid_reconciliation && <BidReconciliation data={derivation.bid_reconciliation} currency={budget.currency} />}

          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
            {FIELD_LABELS.filter(f => derivation[f.key] !== null && derivation[f.key] !== undefined).map(f => (
              <div key={f.key}>
                <div className="text-small text-fg-subtle">{f.label}</div>
                <div className="text-body font-semibold tabular-nums text-fg">{String(derivation[f.key])}</div>
              </div>
            ))}
          </div>

          {assumptionEntries.length > 0 && (
            <div className="border-t border-line pt-1">
              <div className="flex items-center justify-between pb-1 pt-3">
                <h4 className="eyebrow">Assumptions</h4>
                {assumptionEntries.length > 8 && (
                  <Button variant="link" size="sm" onClick={() => setShowAllAssumptions(s => !s)}>
                    {showAllAssumptions ? 'Show fewer' : `Show all ${assumptionEntries.length} assumptions`}
                  </Button>
                )}
              </div>
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
                {visibleAssumptions.map(([key, a]) => (
                  <div key={key} title={a.basis}>
                    <div className="flex items-center gap-1.5">
                      <span className="text-small text-fg-subtle">{humaniseKey(key)}</span>
                      <span className="tag px-1.5 py-0 text-[10px] leading-4">{ASSUMPTION_SOURCE_LABEL[a.source] ?? a.source}</span>
                    </div>
                    <div className="truncate text-body font-semibold tabular-nums text-fg">{String(a.value)}</div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {derivation.trial_precedent && (
            <div className="border-t border-line pt-1">
              <h4 className="eyebrow pb-1 pt-3">Trial precedent</h4>
              {derivation.trial_precedent.found ? (
                <>
                  <p className="mb-1.5 text-small text-fg-muted">
                    Enrollment{derivation.trial_precedent.median_n_sites != null ? ', site,' : ''}
                    {derivation.trial_precedent.median_n_countries != null ? ' and country' : ''} counts
                    above marked <span className="tag px-1.5 py-0 align-middle text-[10px] leading-4">{ASSUMPTION_SOURCE_LABEL.trial_precedent}</span> come
                    from this drug's own real trial history, not a generic template:
                  </p>
                  <ul className="space-y-0.5">
                    {derivation.trial_precedent.trials.map(t => (
                      <li key={t.nct_id} className="text-small text-fg-muted">
                        <span className="font-mono text-fg-subtle">{t.nct_id}</span>
                        {' — '}{t.title}
                        {t.enrollment_count != null && ` · n=${t.enrollment_count}${t.enrollment_type ? ` (${t.enrollment_type.toLowerCase()})` : ''}`}
                        {t.n_countries > 0 && ` · ${t.n_countries} ${t.n_countries === 1 ? 'country' : 'countries'}`}
                        {t.status && ` · ${t.status.toLowerCase().replace(/_/g, ' ')}`}
                      </li>
                    ))}
                  </ul>
                </>
              ) : (
                <p className="text-small text-fg-subtle">
                  No usable ClinicalTrials.gov history was found for this drug — every quantity above
                  falls back to the generic per-study-type default.
                </p>
              )}
            </div>
          )}

          {derivation.conduct && (
            <div className="border-t border-line pt-1">
              <h4 className="eyebrow pb-1 pt-3">Conduct rates</h4>
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
                {CONDUCT_LABELS.map(c => (
                  <div key={c.key}>
                    <div className="text-small text-fg-subtle">{c.label}</div>
                    <div className="text-body font-semibold tabular-nums text-fg">{derivation.conduct![c.key]}</div>
                  </div>
                ))}
              </div>
            </div>
          )}

          <WarningList warnings={derivation.warnings} crossChecks={derivation.cross_checks} />

          <div className="flex flex-wrap items-center gap-3 pt-1">
            {derivation.protocol_linked ? (
              <>
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={handleRelink}
                  disabled={linking}
                  title="Re-prices every row you haven’t edited; your own numbers and deleted rows are left alone."
                >
                  {linking ? 'Refreshing…' : 'Refresh from protocol'}
                </Button>
                <Button
                  variant={confirmUnlink ? 'danger' : 'secondary'}
                  size="sm"
                  onClick={handleUnlinkClick}
                  disabled={linking}
                  title="Line items are kept"
                >
                  {confirmUnlink ? 'Confirm unlink — line items are kept' : 'Unlink'}
                </Button>
              </>
            ) : !picking ? (
              <Button variant="secondary" size="sm" leftIcon={<LinkIcon aria-hidden />} onClick={() => setPicking(true)}>
                Link a protocol for real subject counts
              </Button>
            ) : (
              <LinkPicker
                pickerProtocolId={pickerProtocolId}
                setPickerProtocolId={setPickerProtocolId}
                linking={linking}
                onCancel={() => setPicking(false)}
                onLink={doLink}
              />
            )}
          </div>
        </div>
      )}
    </Card>
  )
}
