import { useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import { USER_INDICATION_MAX_CHARS } from '../types'
import type { Drug, IndicationCandidate, PatientPopulationPreview, StudyType } from '../types'

interface Props {
  drug: Drug
  studyType: StudyType
  value: string
  onChange: (text: string) => void
}

const DEBOUNCE_MS = 700

// Control characters, zero-width / bidi marks and line/paragraph separators are
// stripped client-side; the server sanitises again and is authoritative.
const UNSAFE_CHARS = /[\u0000-\u001F\u007F-\u009F\u200B-\u200F\u2028\u2029]/g

function clean(text: string): string {
  return text.replace(UNSAFE_CHARS, ' ').replace(/ {2,}/g, ' ').slice(0, USER_INDICATION_MAX_CHARS)
}

const SETTING_LABELS: Record<string, string> = {
  adjuvant: 'Adjuvant (post-surgery)',
  low_burden: 'Low tumour burden',
  high_burden: 'Metastatic / high tumour burden',
  not_applicable: 'Non-oncology',
  unknown: 'Setting not established',
}

const MONO_LABELS: Record<string, string> = {
  yes: 'Monotherapy',
  possible: 'Monotherapy possible',
  no: 'With background therapy',
}

function Badge({ tone, children }: { tone: 'emerald' | 'amber' | 'cyan' | 'slate'; children: React.ReactNode }) {
  const cls = {
    emerald: 'text-emerald-700 bg-emerald-50 border-emerald-100',
    amber: 'text-amber-700 bg-amber-50 border-amber-200',
    cyan: 'text-cyan-700 bg-cyan-50 border-cyan-100',
    slate: 'text-slate-600 bg-slate-50 border-slate-200',
  }[tone]
  return <span className={`text-[10px] font-bold border px-1.5 py-0.5 rounded-full ${cls}`}>{children}</span>
}

function CandidateBadges({ c }: { c: IndicationCandidate }) {
  const setting = c.tumor_setting
  const settingTone = setting === 'adjuvant' || setting === 'low_burden' || setting === 'not_applicable'
    ? 'emerald'
    : setting === 'high_burden' ? 'amber' : 'slate'
  const monoTone = c.monotherapy === 'yes' ? 'emerald' : c.monotherapy === 'possible' ? 'cyan' : 'amber'
  return (
    <div className="flex flex-wrap items-center gap-1.5 mt-1.5">
      {c.on_label ? <Badge tone="emerald">On label</Badge> : <Badge tone="amber">Not confirmed on label</Badge>}
      {setting && <Badge tone={settingTone}>{SETTING_LABELS[setting] ?? setting}</Badge>}
      {MONO_LABELS[c.monotherapy] && <Badge tone={monoTone}>{MONO_LABELS[c.monotherapy]}</Badge>}
    </div>
  )
}

export default function PatientIndicationPanel({ drug, studyType, value, onChange }: Props) {
  const [draft, setDraft] = useState(value)
  const [preview, setPreview] = useState<PatientPopulationPreview | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [showAll, setShowAll] = useState(false)

  const onChangeRef = useRef(onChange)
  onChangeRef.current = onChange
  const seq = useRef(0)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const firstDraft = useRef(true)

  const load = (text: string) => {
    const mine = ++seq.current
    setLoading(true)
    setError(null)
    api.indicationPreview(drug.id, studyType, text || undefined)
      .then(res => { if (mine === seq.current) setPreview(res) })
      .catch(e => { if (mine === seq.current) setError(e instanceof Error ? e.message : 'request failed') })
      .finally(() => { if (mine === seq.current) setLoading(false) })
  }

  useEffect(() => {
    load(value)
    return () => {
      seq.current++
      if (timer.current) clearTimeout(timer.current)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [drug.id, studyType])

  const draftRef = useRef(draft)
  draftRef.current = draft
  const pending = useRef(false)

  useEffect(() => {
    if (firstDraft.current) {
      firstDraft.current = false
      return
    }
    if (timer.current) clearTimeout(timer.current)
    pending.current = true
    timer.current = setTimeout(() => {
      pending.current = false
      const text = clean(draft).trim()
      onChangeRef.current(text)
      load(text)
    }, DEBOUNCE_MS)
    return () => { if (timer.current) clearTimeout(timer.current) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draft])

  // Commit immediately (not after the debounce) on blur and on unmount, so
  // typing an indication and clicking Next/Back straight away never drops it.
  const flush = () => {
    if (!pending.current) return
    pending.current = false
    if (timer.current) clearTimeout(timer.current)
    const text = clean(draftRef.current).trim()
    onChangeRef.current(text)
    load(text)
  }
  useEffect(() => () => {
    if (!pending.current) return
    pending.current = false
    onChangeRef.current(clean(draftRef.current).trim())
  }, [])

  const engine = preview?.engine_recommended ?? null
  const ranked = preview?.ranked ?? []
  const unavailable = error !== null || (preview !== null && preview.applicable === false)
  const unavailableReason = error ?? preview?.reason ?? 'not applicable to this product'

  return (
    <div className="section-card px-4 py-3.5 space-y-3">
      <div>
        <span className="text-sm font-semibold text-slate-900">Study indication</span>
        <p className="text-[13px] text-slate-500 leading-snug mt-1">
          Ranked by the engine: on-label → biologic-naive → lowest-variability setting (adjuvant / low tumour burden /
          non-oncology) → monotherapy → lowest PK variability → recruitability. You may enter your own indication
          instead; it is checked against the label and compared with the engine&apos;s pick.
        </p>
      </div>

      <div>
        <div className="flex items-center justify-between">
          <label htmlFor="user-indication" className="text-[11px] font-bold text-slate-500 uppercase tracking-widest">
            Your indication (optional)
          </label>
          <span className="text-[11px] text-slate-400 tabular-nums">{draft.length}/{USER_INDICATION_MAX_CHARS}</span>
        </div>
        <div className="flex gap-2 mt-1.5">
          <input
            id="user-indication"
            type="text"
            maxLength={USER_INDICATION_MAX_CHARS}
            value={draft}
            onChange={e => setDraft(clean(e.target.value))}
            onBlur={flush}
            placeholder="e.g. Resected stage III melanoma (adjuvant, monotherapy)"
            className="input-base flex-1"
          />
          <button
            type="button"
            onClick={() => setDraft('')}
            disabled={draft === ''}
            className="btn-ghost disabled:opacity-40 disabled:cursor-not-allowed"
          >
            Clear
          </button>
        </div>
      </div>

      {loading && !preview && !error && (
        <div className="h-[56px] rounded-xl border border-slate-200 bg-slate-50/60 animate-pulse" />
      )}

      {unavailable && (
        <div className="rounded-xl border border-slate-200 bg-slate-50 px-3.5 py-2.5 text-[13px] text-slate-500">
          Indication ranking unavailable: {unavailableReason}
        </div>
      )}

      {!unavailable && preview && engine && (
        <div className="rounded-xl border border-cyan-100 bg-cyan-50/40 px-3.5 py-2.5">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-sm font-semibold text-slate-900">{engine.indication}</span>
            <Badge tone="cyan">Engine recommendation</Badge>
          </div>
          <CandidateBadges c={engine} />
          {engine.rank_reason && <p className="text-[12px] text-slate-500 leading-snug mt-1.5">{engine.rank_reason}</p>}
        </div>
      )}

      {!unavailable && preview?.user_specified && preview.recommended && (
        <div className="rounded-xl border border-slate-300 bg-white px-3.5 py-2.5">
          <p className="text-[11px] font-bold text-slate-500 uppercase tracking-widest">
            Your indication (locked into the protocol)
          </p>
          <p className="text-sm font-semibold text-slate-900 mt-1">{preview.recommended.indication}</p>
          <CandidateBadges c={preview.recommended} />
          {(preview.user_indication_warnings ?? []).map((w, i) => (
            <div key={i} className="mt-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-[12px] text-amber-800 leading-snug">
              {w}
            </div>
          ))}
          {engine && (
            <p className="text-[12px] text-slate-500 mt-2">
              Engine recommendation for comparison: {engine.indication}
            </p>
          )}
        </div>
      )}

      {!unavailable && ranked.length > 0 && (
        <div>
          <button
            type="button"
            onClick={() => setShowAll(v => !v)}
            className="text-[12px] font-semibold text-cyan-700 hover:text-cyan-800 cursor-pointer"
          >
            {showAll ? 'Hide candidates' : `Show all ${ranked.length} candidates`}
          </button>
          {showAll && (
            <ul className="mt-2 space-y-2">
              {ranked.map((c, i) => (
                <li key={`${c.indication}-${i}`} className="rounded-xl border border-slate-200 px-3.5 py-2.5 flex items-start gap-3">
                  <div className="min-w-0 flex-1">
                    <p className="text-sm font-semibold text-slate-800">{c.indication}</p>
                    <CandidateBadges c={c} />
                    {c.rank_reason && <p className="text-[12px] text-slate-500 leading-snug mt-1.5">{c.rank_reason}</p>}
                  </div>
                  <button
                    type="button"
                    onClick={() => setDraft(i === 0 ? '' : c.indication)}
                    className="btn-ghost flex-shrink-0"
                  >
                    Use this
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  )
}
