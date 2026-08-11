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
    ['Guideline', guidelineLabel],
    ...(pop.additional_pk_timepoints_hours?.trim()
      ? [['Additional PK timepoints', `${pop.additional_pk_timepoints_hours} h`] as [string, string]]
      : []),
    ...(pop.additional_immunogenicity_timepoints_months?.trim()
      ? [['Additional ADA timepoints', `${pop.additional_immunogenicity_timepoints_months} mo`] as [string, string]]
      : []),
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

/** Sentinel for "the client stopped waiting", which is NOT a generation failure.
 *  Kept distinct from any server-supplied generation_error so the UI never
 *  reports a red failure for a protocol that is still being written. */
const STILL_RUNNING = '__still_running__'

// There is no server-side progress signal — the backend runs one long PK
// extraction call, one large single-shot generation call, and (sometimes) a QC
// remediation pass, with no per-section reporting back to the client. So the
// percentage below is a SHAPE, not a measurement: it exists to show visible
// motion, not to claim a real completion fraction. This constant sets only how
// fast that shape climbs — it is deliberately NOT surfaced anywhere as a
// stated time remaining, because a single real timed run (metoprolol,
// crossover BE, still generating past 15 min when checked) is nowhere near
// enough data to promise an ETA, and a wrong ETA is worse than none.
const EXPECTED_GENERATION_SECONDS = 20 * 60

function formatDuration(ms: number): string {
  const totalSec = Math.max(0, Math.round(ms / 1000))
  const m = Math.floor(totalSec / 60)
  const s = totalSec % 60
  return `${m}:${s.toString().padStart(2, '0')}`
}

/** Asymptotic curve: quick early feedback, then slows and creeps toward a 96%
 *  soft ceiling — it NEVER reaches 100 on its own, only the real completion
 *  signal does that. Chosen over a linear ramp specifically because real
 *  generations routinely run past the expected duration (confirmed on the
 *  metoprolol run): a linear bar would either finish "early" and then sit
 *  wrongly at 100%, or blow past 100%. This keeps visibly moving — which reads
 *  as "still working" — no matter how long the run actually takes. */
function estimateProgressPct(elapsedSec: number): number {
  const tau = EXPECTED_GENERATION_SECONDS * 0.6
  return Math.min(96, 96 * (1 - Math.exp(-elapsedSec / tau)))
}

export default function GenerateButton({ drug, studyType, populationConfig, designDecisions, onSuccess }: Props) {
  const [status, setStatus] = useState<Status>('idle')
  const [progressStep, setProgressStep] = useState(0)
  const [elapsedMs, setElapsedMs] = useState(0)
  const [protocol, setProtocol] = useState<GeneratedProtocol | null>(null)
  const [errorMsg, setErrorMsg] = useState('')
  const [guidelines, setGuidelines] = useState<AppGuideline[]>([])
  const [guidelineId, setGuidelineId] = useState('FDA-BE-2014')
  const timerIds = useRef<ReturnType<typeof setTimeout>[]>([])
  const tickIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const genStartRef = useRef<number>(0)
  const mountedRef = useRef(true)
  useEffect(() => {
    // Set true in the SETUP, not just via the useRef() initial value. React
    // StrictMode's dev-mode double-invoke runs setup -> cleanup -> setup again
    // on every mount to surface exactly this bug: the cleanup below sets the
    // ref false, and without this line nothing ever sets it back to true on
    // the second setup, so it stays permanently false for a component that is
    // genuinely mounted. That silently broke the elapsed-time tick (blocked by
    // the same flag) and could abandon a real poll loop on its very first
    // check -- invisible before because nothing rendered a per-second value
    // from it, unlike the progress clock, which made it obvious immediately.
    mountedRef.current = true
    return () => {
      mountedRef.current = false
      if (tickIntervalRef.current) clearInterval(tickIntervalRef.current)
    }
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

  // A biosimilar is a different submission from a generic: the FDA profile's
  // headline citation/pathway/label is ANDA (505(j)) wording that does not
  // apply to a 351(k) programme, so show the biosimilar framing when that is
  // the study - for the selector's own label as well as the citation and
  // pathway text below it, or the label alone would keep reading "ANDA" while
  // the pathway line correctly says "351(k)" right underneath it.
  const isBiosimilar = studyType === 'biosimilar_pk'
  const guidelineDisplayName = (g: AppGuideline) =>
    (isBiosimilar && g.biosimilar_display_name) || g.display_name
  const selectedGuideline = guidelines.find(g => g.id === guidelineId)
  const guidelineLabel = selectedGuideline
    ? `${guidelineDisplayName(selectedGuideline)} · ${selectedGuideline.region}`
    : guidelineId
  const guidelineCitation = selectedGuideline
    ? (isBiosimilar && selectedGuideline.biosimilar_citation) || selectedGuideline.citation
    : ''
  const guidelinePathway = selectedGuideline
    ? (isBiosimilar ? selectedGuideline.biosimilar_pathway : selectedGuideline.submission_pathway) || ''
    : ''

  const clearTimers = () => {
    timerIds.current.forEach(clearTimeout)
    timerIds.current = []
    if (tickIntervalRef.current) {
      clearInterval(tickIntervalRef.current)
      tickIntervalRef.current = null
    }
  }

  // Real generation runs for minutes, so the backend returns a "generating"
  // protocol immediately and finishes it in the background (a single multi-minute
  // HTTP request gets dropped by the edge proxy — the old "Failed to fetch"). Poll
  // until it flips to complete/failed. Transient network blips are tolerated (we
  // just keep polling), which is the whole point of not holding one connection open.
  const pollUntilDone = async (id: string): Promise<GeneratedProtocol> => {
    // ~30 min at 3s. Raised from 200 (10 min) after a real generation was cut
    // off at the old ceiling while the server was still working normally: the
    // nine deterministic engines each inject locked facts that the model must
    // render as tables (sampling schedule, DDI list, safety gating, the BMV
    // validation/stability/handling set, CRF spec, DMP, meta-analysis
    // appendix), so a full protocol now takes substantially longer to write
    // than it did when this ceiling was chosen. The server has no matching
    // timeout -- it finishes and persists regardless -- so a client ceiling
    // that is too low only ever produces a false failure for work that
    // succeeded.
    const MAX_ATTEMPTS = 600
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
    // NOT a failure: the client stopped waiting, the server did not stop working.
    // Tagged so the UI can say that honestly instead of reporting a red
    // "Generation failed" for a protocol that is still being written and will
    // persist normally.
    throw new Error(STILL_RUNNING)
  }

  const handleGenerate = async () => {
    setStatus('loading')
    setProgressStep(0)
    setErrorMsg('')
    genStartRef.current = Date.now()
    setElapsedMs(0)
    tickIntervalRef.current = setInterval(() => {
      if (!mountedRef.current) return
      setElapsedMs(Date.now() - genStartRef.current)
    }, 1000)
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

  const elapsedSec = elapsedMs / 1000
  const progressPct = status === 'success' ? 100 : Math.max(4, estimateProgressPct(elapsedSec))

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
          <p className="text-sm text-slate-500 mt-1">Real generations often run well past 15 minutes — this is normal. Do not close this tab.</p>
        </div>

        <div>
          <div className="flex items-baseline justify-between mb-1.5">
            <span className="text-2xl font-bold text-slate-900 tabular-nums">{Math.round(progressPct)}%</span>
            <span className="text-xs text-slate-400 font-medium tabular-nums">
              Elapsed {formatDuration(elapsedMs)}
            </span>
          </div>
          <div className="w-full bg-slate-100 rounded-full h-1.5 overflow-hidden">
            <div
              className="h-full bg-gradient-to-r from-cyan-500 to-cyan-400 rounded-full transition-all duration-1000 ease-out"
              style={{ width: `${progressPct}%` }}
            />
          </div>
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
            {guidelines.length === 0 && (
              <option value="FDA-BE-2014">
                {isBiosimilar ? 'FDA — Biosimilar PK Similarity (351(k), 2014)' : 'FDA — Bioequivalence (ANDA, 2014)'}
              </option>
            )}
            {guidelines.map(g => (
              <option key={g.id} value={g.id}>
                {guidelineDisplayName(g)} · {g.region}
              </option>
            ))}
          </select>
          <svg width="16" height="16" viewBox="0 0 16 16" fill="none" className="pointer-events-none absolute right-3.5 top-1/2 -translate-y-1/2 text-slate-400">
            <path d="M4 6l4 4 4-4" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </div>
        {selectedGuideline && (
          <p className="mt-1.5 text-[11px] text-slate-400 leading-snug">{guidelineCitation}</p>
        )}
        {guidelinePathway && (
          <p className="mt-1 text-[11px] text-slate-500 leading-snug">
            <span className="font-semibold">Submission pathway:</span> {guidelinePathway}
          </p>
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

      {status === 'error' && errorMsg === STILL_RUNNING && (
        // Amber, not red: nothing failed. The server keeps writing and persists
        // the protocol; only this tab stopped watching.
        <div className="flex items-start gap-3 p-4 bg-amber-50 border border-amber-200 rounded-xl text-sm text-amber-800">
          <svg width="16" height="16" viewBox="0 0 16 16" fill="none" className="flex-shrink-0 mt-0.5">
            <circle cx="8" cy="8" r="7" stroke="#b45309" strokeWidth="1.5" />
            <path d="M8 4.5V8l2.5 1.5" stroke="#b45309" strokeWidth="1.5" strokeLinecap="round" />
          </svg>
          <div>
            <span className="font-semibold">Still generating.</span> This is a long
            protocol and it has exceeded the time this page waits. It has{' '}
            <span className="font-semibold">not</span> failed — generation continues on
            the server and the finished protocol will appear in{' '}
            <span className="font-semibold">My Reports</span>. Open it from there in a
            few minutes; there is no need to generate again.
          </div>
        </div>
      )}

      {status === 'error' && errorMsg !== STILL_RUNNING && (
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
