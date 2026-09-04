import { useState } from 'react'
import { api } from '../api/client'
import BudgetProtocolPicker from './BudgetProtocolPicker'
import { relativeDate } from './budgetFormat'
import type { Budget } from '../types'

interface Props {
  budget: Budget
  /** link-protocol always returns the WHOLE budget — replace state with it. */
  onBudgetUpdated: (b: Budget) => void
  onError: (msg: string) => void
}

function LinkIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 20 20" fill="none">
      <path d="M8.5 11.5a3 3 0 004.24 0l2-2a3 3 0 10-4.24-4.24l-.7.7" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M11.5 8.5a3 3 0 00-4.24 0l-2 2a3 3 0 104.24 4.24l.7-.7" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}
function WarningIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 20 20" fill="none" className="flex-shrink-0 mt-0.5">
      <path d="M10 3.5l7.5 13a1 1 0 01-.87 1.5h-15a1 1 0 01-.87-1.5l7.5-13a1 1 0 011.74 0z" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
      <path d="M10 8.5v3.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
      <circle cx="10" cy="14.5" r="0.9" fill="currentColor" />
    </svg>
  )
}
function ChevronIcon({ open }: { open: boolean }) {
  return (
    <svg width="14" height="14" viewBox="0 0 20 20" fill="none" className={`transition-transform flex-shrink-0 ${open ? 'rotate-180' : ''}`}>
      <path d="M5 8l5 5 5-5" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
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
  default: 'default',
  computed: 'computed',
  inferred_from_modality: 'inferred',
}

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

function WarningList({ warnings }: { warnings: string[] }) {
  if (warnings.length === 0) return null
  return (
    <div className="rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 flex gap-2.5 text-amber-800">
      <WarningIcon />
      <ul className="space-y-1 text-xs list-none flex-1">
        {warnings.map((w, i) => <li key={i}>{w}</li>)}
      </ul>
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
      <div className="rounded-2xl border border-dashed border-slate-300 bg-white/60 px-5 py-4">
        {!picking ? (
          <div className="flex items-center justify-between gap-3 flex-wrap">
            <p className="text-sm text-slate-500">Link a protocol to pull its real cost drivers in.</p>
            <button onClick={() => setPicking(true)} className="btn-ghost text-xs py-1.5 px-3">
              <LinkIcon />
              Link a protocol
            </button>
          </div>
        ) : (
          <div className="space-y-3">
            <div className="flex items-center justify-between">
              <p className="text-sm font-semibold text-slate-700">Link a protocol</p>
              <button onClick={() => setPicking(false)} className="text-xs text-slate-400 hover:text-slate-600 cursor-pointer">
                Cancel
              </button>
            </div>
            <BudgetProtocolPicker value={pickerProtocolId} onChange={id => setPickerProtocolId(id)} disabled={linking} />
            <div className="flex justify-end">
              <button
                onClick={() => pickerProtocolId && doLink(pickerProtocolId)}
                disabled={!pickerProtocolId || linking}
                className="btn-primary text-xs py-1.5 px-3"
              >
                {linking ? 'Linking…' : 'Link this protocol'}
              </button>
            </div>
          </div>
        )}
      </div>
    )
  }

  // Always a full derivation from here on — `skipped` is always false now.
  const assumptions = derivation.assumptions ?? {}
  const assumptionEntries = Object.entries(assumptions)
  const visibleAssumptions = showAllAssumptions || assumptionEntries.length <= 8 ? assumptionEntries : assumptionEntries.slice(0, 8)

  return (
    <div className="card overflow-hidden">
      <button onClick={() => setExpanded(e => !e)} className="w-full flex items-center justify-between gap-3 px-5 py-4 text-left cursor-pointer">
        <div className="flex items-center gap-2 flex-wrap">
          <h3 className="text-sm font-bold text-slate-900">
            {derivation.protocol_linked ? 'From your protocol' : 'How these figures were built'}
          </h3>
          {derivation.protocol_linked ? (
            <span className="text-[10px] font-bold text-cyan-700 bg-cyan-50 border border-cyan-100 px-1.5 py-0.5 rounded-full whitespace-nowrap">
              From protocol
            </span>
          ) : (
            <>
              <span className="text-[10px] font-bold text-slate-500 bg-slate-100 border border-slate-200 px-1.5 py-0.5 rounded-full whitespace-nowrap">
                Estimated
              </span>
              {derivation.rate_card_version && (
                <span className="text-[11px] text-slate-400">Rate card {derivation.rate_card_version}</span>
              )}
            </>
          )}
          <span className="text-[11px] text-slate-400">Derived {relativeDate(derivation.derived_at)}</span>
        </div>
        <ChevronIcon open={expanded} />
      </button>

      {expanded && (
        <div className="px-5 pb-5 space-y-4">
          <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
            {FIELD_LABELS.filter(f => derivation[f.key] !== null && derivation[f.key] !== undefined).map(f => (
              <div key={f.key}>
                <div className="text-[11px] text-slate-400">{f.label}</div>
                <div className="text-sm font-semibold text-slate-800 tabular-nums">{String(derivation[f.key])}</div>
              </div>
            ))}
          </div>

          {assumptionEntries.length > 0 && (
            <div className="pt-1 border-t border-slate-100">
              <div className="flex items-center justify-between pt-3 pb-1">
                <h4 className="text-[11px] font-bold text-slate-500 uppercase tracking-wider">Assumptions</h4>
                {assumptionEntries.length > 8 && (
                  <button
                    onClick={() => setShowAllAssumptions(s => !s)}
                    className="text-[11px] font-semibold text-cyan-600 hover:text-cyan-700 cursor-pointer"
                  >
                    {showAllAssumptions ? 'Show fewer' : `Show all ${assumptionEntries.length} assumptions`}
                  </button>
                )}
              </div>
              <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
                {visibleAssumptions.map(([key, a]) => (
                  <div key={key} title={a.basis}>
                    <div className="flex items-center gap-1.5">
                      <span className="text-[11px] text-slate-400">{humaniseKey(key)}</span>
                      <span className="tag text-[9px] py-0 px-1 leading-4">{ASSUMPTION_SOURCE_LABEL[a.source] ?? a.source}</span>
                    </div>
                    <div className="text-sm font-semibold text-slate-800 tabular-nums truncate">{String(a.value)}</div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {derivation.conduct && (
            <div className="pt-1 border-t border-slate-100">
              <h4 className="text-[11px] font-bold text-slate-500 uppercase tracking-wider pt-3 pb-1">Conduct rates</h4>
              <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
                {CONDUCT_LABELS.map(c => (
                  <div key={c.key}>
                    <div className="text-[11px] text-slate-400">{c.label}</div>
                    <div className="text-sm font-semibold text-slate-800 tabular-nums">{derivation.conduct![c.key]}</div>
                  </div>
                ))}
              </div>
            </div>
          )}

          <WarningList warnings={derivation.warnings} />

          <div className="flex items-center gap-3 pt-1 flex-wrap">
            {derivation.protocol_linked ? (
              <>
                <button
                  onClick={handleRelink}
                  disabled={linking}
                  title="Re-prices every row you haven’t edited; your own numbers and deleted rows are left alone."
                  className="btn-ghost text-xs py-1.5 px-3"
                >
                  {linking ? 'Refreshing…' : 'Refresh from protocol'}
                </button>
                <button
                  onClick={handleUnlinkClick}
                  disabled={linking}
                  title="Line items are kept"
                  className={`text-xs font-semibold px-3 py-1.5 rounded-xl border cursor-pointer ${
                    confirmUnlink ? 'border-red-300 text-red-700 bg-red-50' : 'border-slate-200 text-slate-500 hover:bg-slate-50'
                  }`}
                >
                  {confirmUnlink ? 'Confirm unlink — line items are kept' : 'Unlink'}
                </button>
              </>
            ) : !picking ? (
              <button onClick={() => setPicking(true)} className="btn-ghost text-xs py-1.5 px-3">
                <LinkIcon />
                Link a protocol for real subject counts
              </button>
            ) : (
              <div className="w-full space-y-3">
                <div className="flex items-center justify-between">
                  <p className="text-sm font-semibold text-slate-700">Link a protocol</p>
                  <button onClick={() => setPicking(false)} className="text-xs text-slate-400 hover:text-slate-600 cursor-pointer">
                    Cancel
                  </button>
                </div>
                <BudgetProtocolPicker value={pickerProtocolId} onChange={id => setPickerProtocolId(id)} disabled={linking} />
                <div className="flex justify-end">
                  <button
                    onClick={() => pickerProtocolId && doLink(pickerProtocolId)}
                    disabled={!pickerProtocolId || linking}
                    className="btn-primary text-xs py-1.5 px-3"
                  >
                    {linking ? 'Linking…' : 'Link this protocol'}
                  </button>
                </div>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
