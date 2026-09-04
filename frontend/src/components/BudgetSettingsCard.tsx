import { useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import { api } from '../api/client'
import type { Budget, BudgetUpdatePayload, StudyType } from '../types'

interface Props {
  budget: Budget
  /** A budget-level PATCH returns the WHOLE budget (rollup included) —
   *  the editor just replaces its state with it. */
  onBudgetUpdated: (b: Budget) => void
  onError: (msg: string) => void
}

const STUDY_TYPES: StudyType[] = ['BE', 'biosimilar_pk', 'food_effect']
const STUDY_LABELS: Record<StudyType, string> = {
  BE: 'Bioequivalence',
  biosimilar_pk: 'Biosimilar PK',
  food_effect: 'Food Effect',
}

function FromProtocolPill() {
  return (
    <span className="text-[10px] font-bold text-cyan-700 bg-cyan-50 border border-cyan-100 px-1.5 py-0.5 rounded-full whitespace-nowrap">
      From protocol
    </span>
  )
}
function EditedPill() {
  return (
    <span className="text-[10px] font-bold text-amber-700 bg-amber-50 border border-amber-200 px-1.5 py-0.5 rounded-full whitespace-nowrap">
      Edited
    </span>
  )
}
function AutoEstimatedPill() {
  return (
    <span className="text-[10px] font-bold text-slate-500 bg-slate-100 border border-slate-200 px-1.5 py-0.5 rounded-full whitespace-nowrap">
      Estimated
    </span>
  )
}

/** Which pill (if any) a field gets, per the assumption record rather than
 *  the blanket `linked` flag — 'protocol' -> From protocol, 'user' -> none,
 *  everything else ('default' | 'computed' | 'inferred_from_modality') ->
 *  Estimated. `title` on the label surfaces the server's own `basis` string. */
function fieldPill(budget: Budget, key: string): { pill: ReactNode; title?: string } {
  const a = budget.derivation?.assumptions?.[key]
  if (!a) return { pill: null }
  if (a.source === 'protocol') return { pill: <FromProtocolPill />, title: a.basis }
  if (a.source === 'user') return { pill: null, title: a.basis }
  return { pill: <AutoEstimatedPill />, title: a.basis }
}

function numOrNull(raw: string): number | null {
  const t = raw.trim()
  if (t === '') return null
  const n = Number(t)
  return Number.isFinite(n) ? n : null
}

export default function BudgetSettingsCard({ budget, onBudgetUpdated, onError }: Props) {
  const linked = budget.protocol_id !== null

  // Fields the user has changed via this form since the last (re-)link.
  // There's no stored "originally derived" value for drug_inn/study_type to
  // diff against on every render (only n_enrolled has one, in
  // budget.derivation.n_enrolled) — so this session-local set is how those
  // two fields pick up the "Edited" pill. It resets whenever a fresh link
  // happens (derived_at changes) or the protocol is unlinked entirely.
  const [editedFields, setEditedFields] = useState<Set<string>>(new Set())
  useEffect(() => { setEditedFields(new Set()) }, [budget.derivation?.derived_at, budget.protocol_id])

  const nEnrolledEdited =
    linked && budget.derivation?.n_enrolled != null && budget.n_enrolled !== budget.derivation.n_enrolled

  const [drugInn, setDrugInn] = useState(budget.drug_inn ?? '')
  const [nEnrolled, setNEnrolled] = useState(budget.n_enrolled != null ? String(budget.n_enrolled) : '')
  const [nSites, setNSites] = useState(budget.n_sites != null ? String(budget.n_sites) : '')
  const [nDosingGroups, setNDosingGroups] = useState(budget.n_dosing_groups != null ? String(budget.n_dosing_groups) : '')
  const [screenFailPct, setScreenFailPct] = useState(budget.screen_failure_rate_pct != null ? String(budget.screen_failure_rate_pct) : '')
  const [nScreenFailures, setNScreenFailures] = useState(budget.n_screen_failures != null ? String(budget.n_screen_failures) : '')
  const [nAlternates, setNAlternates] = useState(budget.n_alternates != null ? String(budget.n_alternates) : '')
  const [nScreened, setNScreened] = useState(budget.n_screened != null ? String(budget.n_screened) : '')
  const [projectMonths, setProjectMonths] = useState(budget.project_duration_months != null ? String(budget.project_duration_months) : '')

  function revertDrafts() {
    setDrugInn(budget.drug_inn ?? '')
    setNEnrolled(budget.n_enrolled != null ? String(budget.n_enrolled) : '')
    setNSites(budget.n_sites != null ? String(budget.n_sites) : '')
    setNDosingGroups(budget.n_dosing_groups != null ? String(budget.n_dosing_groups) : '')
    setScreenFailPct(budget.screen_failure_rate_pct != null ? String(budget.screen_failure_rate_pct) : '')
    setNScreenFailures(budget.n_screen_failures != null ? String(budget.n_screen_failures) : '')
    setNAlternates(budget.n_alternates != null ? String(budget.n_alternates) : '')
    setNScreened(budget.n_screened != null ? String(budget.n_screened) : '')
    setProjectMonths(budget.project_duration_months != null ? String(budget.project_duration_months) : '')
  }

  async function patch(field: string, payload: BudgetUpdatePayload) {
    try {
      const updated = await api.updateBudget(budget.id, payload)
      onBudgetUpdated(updated)
      if (linked && (field === 'drug_inn' || field === 'study_type')) {
        setEditedFields(prev => new Set(prev).add(field))
      }
    } catch (e) {
      onError(e instanceof Error ? e.message : 'Failed to save budget settings')
      revertDrafts()
    }
  }

  const drugInnPill = fieldPill(budget, 'drug_inn')
  const studyTypePill = fieldPill(budget, 'study_type')
  const nEnrolledPill = fieldPill(budget, 'n_enrolled')
  const nSitesPill = fieldPill(budget, 'n_sites')
  const nDosingGroupsPill = fieldPill(budget, 'n_dosing_groups')
  const projectMonthsPill = fieldPill(budget, 'project_duration_months')
  const screenFailPctPill = fieldPill(budget, 'screen_failure_rate_pct')
  const nScreenFailuresPill = fieldPill(budget, 'n_screen_failures')
  const nAlternatesPill = fieldPill(budget, 'n_alternates')
  const nScreenedPill = fieldPill(budget, 'n_screened')

  return (
    <div className="card p-6 space-y-6">
      {/* Auto-estimated on create; every one of these is editable. */}
      <div className="space-y-3">
        <div>
          <h3 className="text-sm font-bold text-slate-900">Study details</h3>
          <p className="text-xs text-slate-400 mt-0.5">
            Auto-estimated from the drug and study type — edit any of them.
          </p>
        </div>
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
          <div>
            <label className="flex items-center gap-1.5 text-xs font-semibold text-slate-600 mb-1.5" title={drugInnPill.title}>
              Drug (INN)
              {linked && <FromProtocolPill />}
              {!linked && drugInnPill.pill}
              {editedFields.has('drug_inn') && <EditedPill />}
            </label>
            <input
              value={drugInn}
              onChange={e => setDrugInn(e.target.value)}
              onBlur={() => {
                if (drugInn.trim() !== (budget.drug_inn ?? '')) patch('drug_inn', { drug_inn: drugInn.trim() || null })
              }}
              placeholder="—"
              className="input-base"
            />
          </div>
          <div>
            <label className="flex items-center gap-1.5 text-xs font-semibold text-slate-600 mb-1.5" title={studyTypePill.title}>
              Study type
              {linked && <FromProtocolPill />}
              {!linked && studyTypePill.pill}
              {editedFields.has('study_type') && <EditedPill />}
            </label>
            <select
              value={budget.study_type ?? ''}
              onChange={e => patch('study_type', { study_type: (e.target.value || null) as StudyType | null })}
              className="input-base cursor-pointer"
            >
              <option value="">—</option>
              {STUDY_TYPES.map(s => <option key={s} value={s}>{STUDY_LABELS[s]}</option>)}
            </select>
          </div>
          <div>
            <label className="flex items-center gap-1.5 text-xs font-semibold text-slate-600 mb-1.5" title={nEnrolledPill.title}>
              Subjects enrolled
              {linked && <FromProtocolPill />}
              {!linked && nEnrolledPill.pill}
              {nEnrolledEdited && <EditedPill />}
            </label>
            <input
              value={nEnrolled}
              onChange={e => setNEnrolled(e.target.value)}
              onBlur={() => {
                const n = numOrNull(nEnrolled)
                if (n !== budget.n_enrolled) patch('n_enrolled', { n_enrolled: n })
              }}
              placeholder="—"
              className="input-base tabular-nums"
            />
          </div>
        </div>
      </div>

      {/* Study parameters — also auto-estimated, but re-linking a protocol
       *  never re-derives these from it: adjust the affected rows directly,
       *  or re-link to re-derive everything from scratch. */}
      <div className="space-y-3 pt-2 border-t border-slate-100">
        <div>
          <h3 className="text-sm font-bold text-slate-900">Study parameters</h3>
          <p className="text-xs text-slate-400 mt-0.5">
            Estimated too. Changing one here doesn&apos;t re-price the line items below — adjust the affected rows
            directly, or re-link a protocol to re-derive everything.
          </p>
        </div>
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
          <Field
            label="Sites"
            pill={nSitesPill.pill}
            title={nSitesPill.title}
            value={nSites}
            onChange={setNSites}
            onBlur={() => { const n = numOrNull(nSites); if (n !== budget.n_sites) patch('n_sites', { n_sites: n }) }}
          />
          <Field
            label="Dosing groups"
            pill={nDosingGroupsPill.pill}
            title={nDosingGroupsPill.title}
            value={nDosingGroups}
            onChange={setNDosingGroups}
            onBlur={() => { const n = numOrNull(nDosingGroups); if (n !== budget.n_dosing_groups) patch('n_dosing_groups', { n_dosing_groups: n }) }}
          />
          <Field
            label="Project duration (months)"
            pill={projectMonthsPill.pill}
            title={projectMonthsPill.title}
            value={projectMonths}
            onChange={setProjectMonths}
            onBlur={() => { const n = numOrNull(projectMonths); if (n !== budget.project_duration_months) patch('project_duration_months', { project_duration_months: n }) }}
          />
          <Field
            label="Screen failure rate (%)"
            pill={screenFailPctPill.pill}
            title={screenFailPctPill.title}
            value={screenFailPct}
            onChange={setScreenFailPct}
            onBlur={() => { const n = numOrNull(screenFailPct); if (n !== budget.screen_failure_rate_pct) patch('screen_failure_rate_pct', { screen_failure_rate_pct: n }) }}
          />
          <Field
            label="Screen failures"
            pill={nScreenFailuresPill.pill}
            title={nScreenFailuresPill.title}
            value={nScreenFailures}
            onChange={setNScreenFailures}
            onBlur={() => { const n = numOrNull(nScreenFailures); if (n !== budget.n_screen_failures) patch('n_screen_failures', { n_screen_failures: n }) }}
          />
          <Field
            label="Alternates"
            pill={nAlternatesPill.pill}
            title={nAlternatesPill.title}
            value={nAlternates}
            onChange={setNAlternates}
            onBlur={() => { const n = numOrNull(nAlternates); if (n !== budget.n_alternates) patch('n_alternates', { n_alternates: n }) }}
          />
          <Field
            label="Screened"
            pill={nScreenedPill.pill}
            title={nScreenedPill.title}
            value={nScreened}
            onChange={v => setNScreened(v)}
            onBlur={() => {
              const n = numOrNull(nScreened)
              if (n !== budget.n_screened) patch('n_screened', { n_screened: n })
            }}
          />
        </div>
      </div>
    </div>
  )
}

function Field({ label, pill, title, value, onChange, onBlur }: {
  label: string
  pill?: ReactNode
  title?: string
  value: string
  onChange: (v: string) => void
  onBlur: () => void
}) {
  return (
    <div>
      <label className="flex items-center gap-1.5 text-xs font-semibold text-slate-600 mb-1.5" title={title}>
        {label}
        {pill}
      </label>
      <input
        value={value}
        onChange={e => onChange(e.target.value)}
        onBlur={onBlur}
        placeholder="—"
        className="input-base tabular-nums"
      />
    </div>
  )
}
