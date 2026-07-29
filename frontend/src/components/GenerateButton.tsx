import { useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import type { AppGuideline, DesignDecision, Drug, GeneratedProtocol, PopulationConfig, StudyType } from '../types'

interface Props {
  drug: Drug
  studyType: StudyType
  populationConfig: PopulationConfig
  designDecisions: DesignDecision[]
  onSuccess: (protocol: GeneratedProtocol) => void
}

type Status = 'idle' | 'loading' | 'success' | 'error'

const PROGRESS_STEPS = [
  {
    label: 'Searching PubMed for PK literature',
    sublabel: 'NCBI E-utilities · DailyMed SPL labels',
  },
  {
    label: 'Extracting pharmacokinetic parameters',
    sublabel: 'Claude tool-use · structured extraction · PMID validation',
  },
  {
    label: 'Generating protocol sections',
    sublabel: 'FDA guideline-mapped prompts · concurrent generation',
  },
  {
    label: 'Saving to database',
    sublabel: 'PostgreSQL · 21 CFR Part 11 audit trail',
  },
]

const STUDY_LABELS: Record<StudyType, string> = {
  BE: 'Bioequivalence (Small Molecule)',
  biosimilar_pk: 'Biosimilar PK Similarity (Biologic)',
  food_effect: 'Food Effect',
}

const SEX_LABELS: Record<string, string> = {
  male: 'Male only',
  female: 'Female only',
  both: 'Both sexes',
}

function CheckCircle({ size = 20 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 20 20" fill="none" className="flex-shrink-0">
      <circle cx="10" cy="10" r="10" fill="#10b981" />
      <path d="M5.5 10l3 3 6-6" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}

function SpinnerCircle({ size = 20 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 20 20" fill="none" className="animate-spin flex-shrink-0">
      <circle cx="10" cy="10" r="8" stroke="#e0f2fe" strokeWidth="3" />
      <path d="M10 2a8 8 0 018 8" stroke="#0891b2" strokeWidth="3" strokeLinecap="round" />
    </svg>
  )
}

function PendingDot({ n }: { n: number }) {
  return (
    <div className="w-5 h-5 rounded-full border-2 border-slate-200 flex items-center justify-center flex-shrink-0">
      <span className="text-[10px] text-slate-400 font-semibold">{n}</span>
    </div>
  )
}

function ReviewTable({ drug, studyType, pop, guidelineLabel }: { drug: Drug; studyType: StudyType; pop: PopulationConfig; guidelineLabel: string }) {
  const rows: [string, string][] = [
    ['Drug (INN)', drug.inn],
    ...(drug.brand_names.length > 0 ? [['Brand names', drug.brand_names.join(' · ')] as [string, string]] : []),
    ['Study type', STUDY_LABELS[studyType]],
    ['Subject type', pop.subject_type === 'healthy' ? 'Healthy volunteers' : 'Patients'],
    ['Age range', `${pop.age_min}–${pop.age_max} years`],
    ['Sex', SEX_LABELS[pop.sex]],
    ['Weight range', `${pop.weight_min}–${pop.weight_max} kg`],
    ['Renal status', pop.renal_status],
    ['Hepatic status', pop.hepatic_status],
    ['Guideline', guidelineLabel],
  ]

  return (
    <div className="rounded-xl border border-slate-200 overflow-hidden divide-y divide-slate-100 text-sm">
      {rows.map(([label, val]) => (
        <div key={label} className="flex gap-4 px-4 py-2.5 odd:bg-slate-50/60">
          <span className="w-32 flex-shrink-0 text-slate-500 text-xs font-semibold uppercase tracking-wide">{label}</span>
          <span className="text-slate-800 font-medium capitalize text-xs">{val}</span>
        </div>
      ))}
    </div>
  )
}

export default function GenerateButton({ drug, studyType, populationConfig, designDecisions, onSuccess }: Props) {
  const [status, setStatus] = useState<Status>('idle')
  const [progressStep, setProgressStep] = useState(0)
  const [protocol, setProtocol] = useState<GeneratedProtocol | null>(null)
  const [errorMsg, setErrorMsg] = useState('')
  const [guidelines, setGuidelines] = useState<AppGuideline[]>([])
  const [guidelineId, setGuidelineId] = useState('FDA-BE-2014')
  const timerIds = useRef<ReturnType<typeof setTimeout>[]>([])
  const mountedRef = useRef(true)
  useEffect(() => {
    return () => { mountedRef.current = false }
  }, [])

  useEffect(() => {
    let cancelled = false
    api.listGuidelines().then(res => {
      if (cancelled) return
      setGuidelines(res.guidelines)
      setGuidelineId(res.default)
    }).catch(() => { /* selector falls back to the FDA default */ })
    return () => { cancelled = true }
  }, [])

  const selectedGuideline = guidelines.find(g => g.id === guidelineId)
  const guidelineLabel = selectedGuideline
    ? `${selectedGuideline.display_name} · ${selectedGuideline.region}`
    : guidelineId

  const clearTimers = () => {
    timerIds.current.forEach(clearTimeout)
    timerIds.current = []
  }

  // Real generation runs for minutes, so the backend returns a "generating"
  // protocol immediately and finishes it in the background (a single multi-minute
  // HTTP request gets dropped by the edge proxy — the old "Failed to fetch"). Poll
  // until it flips to complete/failed. Transient network blips are tolerated (we
  // just keep polling), which is the whole point of not holding one connection open.
  const pollUntilDone = async (id: string): Promise<GeneratedProtocol> => {
    const MAX_ATTEMPTS = 200 // ~10 min at 3s
    const INTERVAL_MS = 3000
    for (let attempt = 0; attempt < MAX_ATTEMPTS; attempt++) {
      if (!mountedRef.current) throw new Error('__abandoned__')
      await new Promise(r => setTimeout(r, INTERVAL_MS))
      let p: GeneratedProtocol
      try {
        p = await api.getProtocol(id)
      } catch {
        continue // transient blip — the background job is still running server-side
      }
      if (p.generation_status === 'failed') {
        throw new Error(p.generation_error || 'Generation failed. Please try again.')
      }
      if (p.generation_status === 'complete' || p.generation_status == null) {
        return p
      }
      // still 'generating' — hold the progress bar near the end
      setProgressStep(s => Math.max(s, 2))
    }
    throw new Error(
      'Generation is taking longer than expected. It may still finish — check "My Reports" in a few minutes.',
    )
  }

  const handleGenerate = async () => {
    setStatus('loading')
    setProgressStep(0)
    setErrorMsg('')
    timerIds.current.push(setTimeout(() => setProgressStep(p => Math.max(p, 1)), 2500))
    timerIds.current.push(setTimeout(() => setProgressStep(p => Math.max(p, 2)), 6500))
    try {
      let result = await api.createProtocol({
        drug_id: drug.id,
        study_type: studyType,
        population_config: populationConfig,
        guideline_version: guidelineId,
        design_decisions: designDecisions,
      })
      // Production: generation runs in the background — poll until it's done.
      // Tests / any fast path return 'complete' immediately and skip polling.
      if (result.generation_status === 'generating') {
        result = await pollUntilDone(result.id)
      }
      if (!mountedRef.current) return
      clearTimers()
      setProgressStep(3)
      await new Promise(r => setTimeout(r, 450))
      if (!mountedRef.current) return
      setProtocol(result)
      setStatus('success')
      onSuccess(result)
    } catch (err: unknown) {
      if (!mountedRef.current) return
      clearTimers()
      setErrorMsg(err instanceof Error ? err.message : 'An unexpected error occurred')
      setStatus('error')
    }
  }

  const handleDownload = async () => {
    if (!protocol) return
    try {
      const blob = await api.exportDocx(protocol.id)
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `protocol_${drug.inn}_v${protocol.version}.docx`
      document.body.appendChild(a)
      a.click()
      document.body.removeChild(a)
      URL.revokeObjectURL(url)
    } catch (e) {
      console.error('Export failed:', e)
    }
  }

  const progressPct = status === 'success' ? 100 : [4, 28, 55, 90][progressStep] ?? 4

  // ── Success ────────────────────────────────────────────────────────────────
  if (status === 'success' && protocol) {
    return (
      <div className="text-center space-y-6">
        <div className="w-16 h-16 bg-emerald-100 rounded-full flex items-center justify-center mx-auto border border-emerald-200">
          <svg width="32" height="32" viewBox="0 0 32 32" fill="none">
            <path d="M6 16l7 7 13-13" stroke="#059669" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </div>

        <div>
          <h3 className="text-xl font-bold text-slate-900">Protocol Generated</h3>
          <p className="text-slate-500 text-sm mt-1 font-medium">
            {Object.keys(protocol.sections).length} sections · Version {protocol.version} · {protocol.status}
          </p>
        </div>

        <div className="bg-slate-50 rounded-xl border border-slate-200 p-5 text-left space-y-3">
          <div>
            <p className="text-[10px] font-bold text-slate-400 uppercase tracking-widest">Protocol ID</p>
            <p className="font-mono text-xs text-slate-700 mt-1 break-all">{protocol.id}</p>
          </div>
          <div>
            <p className="text-[10px] font-bold text-slate-400 uppercase tracking-widest">Guideline</p>
            <p className="text-xs text-slate-600 mt-1 font-medium">{protocol.guideline_version}</p>
          </div>
          <div>
            <p className="text-[10px] font-bold text-slate-400 uppercase tracking-widest">Sections Generated</p>
            <p className="text-xs text-slate-600 mt-1 font-medium">{Object.keys(protocol.sections).length} sections complete</p>
          </div>
        </div>

        <div className="flex gap-3 justify-center flex-wrap">
          <button onClick={handleDownload} className="btn-primary">
            <svg width="15" height="15" viewBox="0 0 15 15" fill="none">
              <path d="M7.5 2v8M4 7l3.5 3.5L11 7" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" />
              <path d="M2 13h11" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" />
            </svg>
            Download .docx
          </button>
          <button
            onClick={() => { setStatus('idle'); setProtocol(null); setProgressStep(0) }}
            className="btn-ghost"
          >
            Generate Another
          </button>
        </div>
      </div>
    )
  }

  // ── Loading ────────────────────────────────────────────────────────────────
  if (status === 'loading') {
    return (
      <div className="space-y-6">
        <div>
          <h3 className="text-base font-bold text-slate-900">Generating Protocol…</h3>
          <p className="text-sm text-slate-500 mt-1">This may take 4–8 minutes (drafting, automated QC, and AI fixes). Do not close this tab.</p>
        </div>

        <div className="w-full bg-slate-100 rounded-full h-1.5 overflow-hidden">
          <div
            className="h-full bg-gradient-to-r from-cyan-500 to-cyan-400 rounded-full transition-all duration-1000 ease-out"
            style={{ width: `${progressPct}%` }}
          />
        </div>

        <div className="space-y-4">
          {PROGRESS_STEPS.map((step, idx) => {
            const done = idx < progressStep
            const active = idx === progressStep
            return (
              <div key={idx} className="flex items-start gap-3">
                {done ? <CheckCircle /> : active ? <SpinnerCircle /> : <PendingDot n={idx + 1} />}
                <div>
                  <p className={[
                    'text-sm font-semibold leading-tight',
                    done ? 'text-emerald-600' : active ? 'text-slate-900' : 'text-slate-400',
                  ].join(' ')}>
                    {step.label}
                    {active && idx === 2 && (
                      <span className="ml-1.5 text-cyan-400 animate-pulse tracking-widest">···</span>
                    )}
                  </p>
                  <p className={[
                    'text-xs mt-0.5',
                    done ? 'text-emerald-500' : active ? 'text-slate-500' : 'text-slate-300',
                  ].join(' ')}>
                    {step.sublabel}
                  </p>
                </div>
              </div>
            )
          })}
        </div>
      </div>
    )
  }

  // ── Idle ───────────────────────────────────────────────────────────────────
  return (
    <div className="space-y-6">
      <div>
        <h3 className="text-base font-bold text-slate-900">Review Configuration</h3>
        <p className="text-sm text-slate-500 mt-1">
          Confirm settings before generating. A three-stage Claude pipeline (Haiku → Opus → Sonnet) will classify, draft, and validate the protocol.
        </p>
      </div>

      {/* Regulatory guideline selector — drives the terminology, acceptance
          framing, and citations the protocol is written against. */}
      <div>
        <label htmlFor="guideline" className="block text-[11px] font-bold text-slate-500 uppercase tracking-widest mb-1.5">
          Regulatory guideline
        </label>
        <div className="relative">
          <select
            id="guideline"
            value={guidelineId}
            onChange={e => setGuidelineId(e.target.value)}
            className="w-full appearance-none rounded-xl border border-slate-200 bg-white px-4 py-3 pr-10 text-sm font-semibold text-slate-800 shadow-sm cursor-pointer focus:outline-none focus:ring-2 focus:ring-cyan-400/40 focus:border-cyan-300 transition"
          >
            {guidelines.length === 0 && <option value="FDA-BE-2014">FDA — Bioequivalence (ANDA, 2014)</option>}
            {guidelines.map(g => (
              <option key={g.id} value={g.id}>
                {g.display_name} · {g.region}
              </option>
            ))}
          </select>
          <svg width="16" height="16" viewBox="0 0 16 16" fill="none" className="pointer-events-none absolute right-3.5 top-1/2 -translate-y-1/2 text-slate-400">
            <path d="M4 6l4 4 4-4" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </div>
        {selectedGuideline && (
          <p className="mt-1.5 text-[11px] text-slate-400 leading-snug">{selectedGuideline.citation}</p>
        )}
      </div>

      <ReviewTable drug={drug} studyType={studyType} pop={populationConfig} guidelineLabel={guidelineLabel} />

      {designDecisions.length > 0 && (
        <div>
          <p className="text-[11px] font-bold text-slate-500 uppercase tracking-widest mb-1.5">Study design</p>
          <div className="rounded-xl border border-slate-200 overflow-hidden divide-y divide-slate-100 text-sm">
            {designDecisions.map(d => (
              <div key={d.key} className="flex gap-4 px-4 py-2.5 odd:bg-slate-50/60">
                <span className="w-32 flex-shrink-0 text-slate-500 text-xs font-semibold uppercase tracking-wide">{d.label}</span>
                <span className="text-slate-800 font-medium text-xs">{d.value}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {status === 'error' && (
        <div className="flex items-start gap-3 p-4 bg-red-50 border border-red-200 rounded-xl text-sm text-red-700">
          <svg width="16" height="16" viewBox="0 0 16 16" fill="none" className="flex-shrink-0 mt-0.5">
            <circle cx="8" cy="8" r="7" stroke="#dc2626" strokeWidth="1.5" />
            <path d="M8 4.5v4M8 10.5v.5" stroke="#dc2626" strokeWidth="1.5" strokeLinecap="round" />
          </svg>
          <div>
            <span className="font-semibold">Generation failed:</span> {errorMsg}
          </div>
        </div>
      )}

      <button
        onClick={handleGenerate}
        className="w-full py-3.5 bg-cyan-600 hover:bg-cyan-700 active:bg-cyan-800 text-white rounded-xl font-bold text-sm transition-all duration-200 shadow-sm flex items-center justify-center gap-2 cursor-pointer"
      >
        {status === 'error' ? 'Retry Generation' : 'Generate Protocol'}
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
          <path d="M3 8h10M9 4l4 4-4 4" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </button>

      <p className="text-[11px] text-center text-slate-400 font-medium">
        Powered by Claude (Haiku · Opus · Sonnet) · FDA BE 2014 · 21 CFR Part 11
      </p>
    </div>
  )
}
