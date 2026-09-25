import { useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import PatientIndicationPanel from './PatientIndicationPanel'
import { DOSE_MULTIPLE_SS, DOSE_SINGLE, POP_PATIENTS_MULTI } from '../types'
import type { DesignDecision, Drug, PopulationConfig, StudyType } from '../types'

interface Props {
  drug: Drug
  studyType: StudyType
  populationConfig: PopulationConfig
  /** Lifted decision state (null until the preview loads). */
  decisions: DesignDecision[] | null
  onChange: (decisions: DesignDecision[]) => void
  /** Lets this step write the user-specified indication back into the config. */
  onPopulationConfigChange: (config: PopulationConfig) => void
}

const ICONS: Record<string, string> = {
  population: 'M10 9a3 3 0 100-6 3 3 0 000 6zM3 19c0-3.87 3.13-7 7-7s7 3.13 7 7',
  dose: 'M9 3h6M10 3v5l-5 10a2 2 0 001.8 3h6.4a2 2 0 001.8-3l-5-10V3M7.5 14h5',
  route: 'M10 2l2 2-6 6-2-2 6-6zM8 6l6 6M4 12l-2 6 6-2',
  design: 'M7 4l-4 4 4 4M13 4l4 4-4 4M4 8h13',
  pd_endpoint: 'M3 12h4l2-7 3 14 2-7h4',
}

function DecisionIcon({ path }: { path: string }) {
  return (
    <svg width="18" height="18" viewBox="0 0 20 20" fill="none">
      <path d={path} stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}

export default function ReviewDesign({ drug, studyType, populationConfig, decisions, onChange, onPopulationConfigChange }: Props) {
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  // Fetch the auto-determined decisions once (credit-free). Re-fetch only if the
  // drug or study type changes (population edits don't change the design calls).
  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    api.designPreview(drug.id, studyType, populationConfig)
      .then(res => { if (!cancelled) onChange(res.decisions) })
      .catch(e => { if (!cancelled) setError(e instanceof Error ? e.message : 'Failed to load design') })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [drug.id, studyType])

  const decisionsRef = useRef<DesignDecision[] | null>(decisions)
  decisionsRef.current = decisions
  const previewSeq = useRef(0)

  const setValue = (key: string, value: string) => {
    if (!decisions) return
    if (key !== 'population') {
      onChange(decisions.map(d => (d.key === key ? { ...d, value } : d)))
      return
    }

    // Population drives the dosing regimen: keep Dose consistent locally right
    // away, then let the server recompute Dose and the PD endpoint for the
    // chosen population (the backend also reconciles again at generation).
    const dose = decisions.find(d => d.key === 'dose')
    let doseValue = dose?.value
    if (dose) {
      if (value === POP_PATIENTS_MULTI && dose.value === DOSE_SINGLE && dose.options.includes(DOSE_MULTIPLE_SS)) {
        doseValue = DOSE_MULTIPLE_SS
      } else if (value !== POP_PATIENTS_MULTI && dose.value === DOSE_MULTIPLE_SS && dose.options.includes(DOSE_SINGLE)) {
        doseValue = DOSE_SINGLE
      }
    }
    const local = decisions.map(d => {
      if (d.key === 'population') return { ...d, value }
      if (d.key === 'dose' && doseValue !== undefined) return { ...d, value: doseValue }
      return d
    })
    decisionsRef.current = local
    onChange(local)

    const seq = ++previewSeq.current
    api.designPreview(drug.id, studyType, populationConfig, value)
      .then(res => {
        if (seq !== previewSeq.current || !decisionsRef.current) return
        const fresh = new Map(res.decisions.map(d => [d.key, d]))
        const merged = decisionsRef.current.map(d =>
          (d.key === 'dose' || d.key === 'pd_endpoint') && fresh.has(d.key) ? fresh.get(d.key)! : d,
        )
        decisionsRef.current = merged
        onChange(merged)
      })
      .catch(() => { /* keep the local state; the backend reconciles at generation */ })
  }

  if (error) {
    return (
      <div className="rounded-xl border border-red-200 bg-red-50 p-4 text-sm text-red-700">
        Couldn&apos;t determine the study design: {error}
      </div>
    )
  }

  if (loading || decisions === null) {
    return (
      <div className="space-y-2.5">
        {[0, 1, 2, 3, 4].map(i => (
          <div key={i} className="h-[74px] rounded-xl border border-slate-200 bg-slate-50/60 animate-pulse" />
        ))}
      </div>
    )
  }

  const populationValue = decisions.find(d => d.key === 'population')?.value ?? ''

  return (
    <div className="space-y-2.5">
      <p className="text-sm text-slate-500 leading-relaxed -mt-1 mb-1">
        Determined from the product&apos;s safety profile, label, and PK. Change any before generating — each
        decision is written into the protocol with its rationale.
      </p>
      {decisions.map(d => (
        <div key={d.key} className="section-card flex items-start gap-3.5 px-4 py-3.5">
          <span className="flex-shrink-0 w-9 h-9 rounded-lg bg-cyan-50 border border-cyan-100 text-cyan-600 flex items-center justify-center mt-0.5">
            <DecisionIcon path={ICONS[d.key] ?? ICONS.design} />
          </span>
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2">
              <span className="text-sm font-semibold text-slate-900">{d.label}</span>
              <span className="text-[10px] font-bold text-cyan-700 bg-cyan-50 border border-cyan-100 px-1.5 py-0.5 rounded-full">Auto</span>
            </div>
            <p className="text-[13px] text-slate-500 leading-snug mt-1">{d.rationale}</p>
            {d.options.length > 1 ? (
              <select
                value={d.value}
                onChange={e => setValue(d.key, e.target.value)}
                className="input-base mt-2 cursor-pointer"
                aria-label={d.label}
              >
                {d.options.map(o => <option key={o} value={o}>{o}</option>)}
              </select>
            ) : (
              <p className="mt-2 text-sm font-semibold text-slate-800">{d.value}</p>
            )}
            {d.key === 'population' && d.value === POP_PATIENTS_MULTI && (
              <p className="mt-1.5 text-[12px] text-slate-500">
                PK is evaluated at steady state under the labelled multiple-dose regimen (first-dose and steady-state AUC(0-tau) co-primary).
              </p>
            )}
          </div>
        </div>
      ))}
      {studyType === 'biosimilar_pk' && populationValue.startsWith('Patients') && (
        <PatientIndicationPanel
          drug={drug}
          studyType={studyType}
          value={populationConfig.user_indication ?? ''}
          onChange={text => onPopulationConfigChange({ ...populationConfig, user_indication: text || undefined })}
        />
      )}
    </div>
  )
}
