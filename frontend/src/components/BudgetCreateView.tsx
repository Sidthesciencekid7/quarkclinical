import { useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import BudgetProtocolPicker from './BudgetProtocolPicker'
import type { Budget, BudgetCreatePayload, BudgetGeographyProfile, BudgetProfilePreview, BudgetSubjectProfile, ProtocolSummary, StudyType } from '../types'

interface Props {
  /** The full budget the server returned, handed straight to the editor —
   *  no second fetch. */
  onCreated: (budget: Budget) => void
  onCancel: () => void
}

const CURRENCIES = ['USD', 'EUR', 'GBP', 'CAD', 'AUD', 'CHF', 'JPY']

const STUDY_TYPES: StudyType[] = ['BE', 'biosimilar_pk', 'food_effect']
const STUDY_LABELS: Record<StudyType, string> = {
  BE: 'Bioequivalence',
  biosimilar_pk: 'Biosimilar PK',
  food_effect: 'Food Effect',
}

/** 'auto' sends subject_profile: undefined — the server resolves it from the
 *  drug KB / linked protocol. The other two are an explicit override. */
type PopulationChoice = 'auto' | BudgetSubjectProfile

const POPULATION_OPTIONS: { id: PopulationChoice; label: string }[] = [
  { id: 'auto', label: 'Auto-detect' },
  { id: 'healthy', label: 'Healthy volunteers' },
  { id: 'patient', label: 'Patients' },
]

type TherapeuticAreaChoice = '' | 'oncology' | 'other'

const GEOGRAPHY_LABELS: Record<BudgetGeographyProfile, string> = {
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
const GEOGRAPHY_OPTIONS: BudgetGeographyProfile[] = [
  'global_emerging_mixed', 'global_developed_mixed', 'us_only', 'eu_west', 'eu_east', 'latam', 'apac', 'china', 'india',
]

function LeftArrowIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
      <path d="M11 7H3M6 3L2 7l4 4" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}

function LinkIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 20 20" fill="none">
      <path d="M8.5 11.5a3 3 0 004.24 0l2-2a3 3 0 10-4.24-4.24l-.7.7" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M11.5 8.5a3 3 0 00-4.24 0l-2 2a3 3 0 104.24 4.24l.7-.7" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}

function ChevronIcon({ open }: { open: boolean }) {
  return (
    <svg
      width="13"
      height="13"
      viewBox="0 0 20 20"
      fill="none"
      className={`transition-transform ${open ? 'rotate-180' : ''}`}
    >
      <path d="M5 8l5 5 5-5" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}

function InfoIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 20 20" fill="none" className="flex-shrink-0 mt-0.5">
      <circle cx="10" cy="10" r="7.25" stroke="currentColor" strokeWidth="1.5" />
      <path d="M10 9v4.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
      <circle cx="10" cy="6.5" r="0.9" fill="currentColor" />
    </svg>
  )
}

function Spinner({ size = 28 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none" className="animate-spin mx-auto">
      <circle cx="8" cy="8" r="6" stroke="#e0f2fe" strokeWidth="2.5" />
      <path d="M8 2a6 6 0 016 6" stroke="#0891b2" strokeWidth="2.5" strokeLinecap="round" />
    </svg>
  )
}

/** The inline "here's what we'll build" hint under the Population control.
 *  Composed from the profile-preview response rather than any hardcoded
 *  drug-specific copy, so it works for every drug the KB knows about. */
function previewHint(drugInn: string, preview: BudgetProfilePreview): string {
  const d = preview.defaults
  const drugLabel = drugInn.trim() ? drugInn.trim()[0].toUpperCase() + drugInn.trim().slice(1) : 'This drug'
  if (preview.subject_profile === 'patient') {
    const bits = [
      d.n_sites != null ? `~${d.n_sites} sites` : null,
      d.n_countries != null ? `${d.n_countries} countries` : null,
      d.project_duration_months != null ? `${d.project_duration_months} months` : null,
    ].filter(Boolean).join(', ')
    return `${drugLabel} — ${preview.subject_profile_basis} We'll build a patient study budget${bits ? ` (${bits})` : ''}.`
  }
  const bits = [
    d.n_sites != null ? `${d.n_sites} site${d.n_sites === 1 ? '' : 's'}` : null,
    d.n_enrolled != null ? `${d.n_enrolled} subjects` : null,
    d.project_duration_months != null ? `${d.project_duration_months} months` : null,
  ].filter(Boolean).join(', ')
  return `${drugLabel} — ${preview.subject_profile_basis} We'll build a healthy-volunteer budget${bits ? ` (${bits})` : ''}.`
}

export default function BudgetCreateView({ onCreated, onCancel }: Props) {
  const [drugInn, setDrugInn] = useState('')
  const [studyType, setStudyType] = useState<StudyType | ''>('')
  const [name, setName] = useState('')
  const [sponsorName, setSponsorName] = useState('')
  const [croName, setCroName] = useState('')
  const [currency, setCurrency] = useState('USD')
  const [protocolId, setProtocolId] = useState<string | null>(null)
  const [protocolLabel, setProtocolLabel] = useState<string | null>(null)
  const [showProtocol, setShowProtocol] = useState(false)
  const [showDetails, setShowDetails] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // ── Population (§6.2) ────────────────────────────────────────────────
  const [population, setPopulation] = useState<PopulationChoice>('auto')
  const [preview, setPreview] = useState<BudgetProfilePreview | null>(null)
  const [previewLoading, setPreviewLoading] = useState(false)
  const debounceRef = useRef<number | null>(null)
  const previewSeq = useRef(0)

  // Patient-only optional block — collapsed by default, genuinely optional:
  // every field falls back to the profile default when left blank.
  const [showPatientDetails, setShowPatientDetails] = useState(false)
  const [indication, setIndication] = useState('')
  const [therapeuticArea, setTherapeuticArea] = useState<TherapeuticAreaChoice>('')
  const [nSitesSelected, setNSitesSelected] = useState('')
  const [nCountries, setNCountries] = useState('')
  const [nEnrolledPatients, setNEnrolledPatients] = useState('')
  const [geographyProfile, setGeographyProfile] = useState<BudgetGeographyProfile | ''>('')

  const trimmedDrug = drugInn.trim()
  const trimmedName = name.trim()
  const canSubmit = (trimmedDrug.length > 0 || trimmedName.length > 0) && !submitting

  // Debounced, credit-free profile-preview lookup while the user types a
  // drug name — the key UX move so a typed drug name never surprises the
  // user with a multi-million-dollar budget.
  useEffect(() => {
    if (debounceRef.current) window.clearTimeout(debounceRef.current)
    if (!trimmedDrug) {
      setPreview(null)
      setPreviewLoading(false)
      return
    }
    setPreviewLoading(true)
    debounceRef.current = window.setTimeout(() => {
      const seq = ++previewSeq.current
      api.budgetProfilePreview(trimmedDrug)
        .then(p => { if (seq === previewSeq.current) { setPreview(p); setPreviewLoading(false) } })
        .catch(() => { if (seq === previewSeq.current) { setPreview(null); setPreviewLoading(false) } })
    }, 400)
    return () => { if (debounceRef.current) window.clearTimeout(debounceRef.current) }
  }, [trimmedDrug])

  function handleProtocolChange(id: string | null, summary: ProtocolSummary | null) {
    setProtocolId(id)
    setProtocolLabel(summary ? `${summary.drug_inn} · ${STUDY_LABELS[summary.study_type]}` : null)
    if (summary) {
      if (!drugInn.trim()) setDrugInn(summary.drug_inn) // never clobber a typed drug name
      if (!studyType) setStudyType(summary.study_type)
    }
  }

  function clearProtocol() {
    setShowProtocol(false)
    setProtocolId(null)
    setProtocolLabel(null)
  }

  function numOrUndefined(raw: string): number | undefined {
    const t = raw.trim()
    if (t === '') return undefined
    const n = Number(t)
    return Number.isFinite(n) ? n : undefined
  }

  async function handleSubmit() {
    if (!canSubmit) return
    setSubmitting(true)
    setError(null)
    // Blank optional fields are omitted (undefined), never sent as '' — the
    // server distinguishes "not provided" from "explicitly cleared".
    const payload: BudgetCreatePayload = {
      name: trimmedName || undefined, // omitted -> server names it "<Drug> study budget"
      drug_inn: trimmedDrug || undefined,
      study_type: studyType || undefined,
      protocol_id: protocolId ?? undefined,
      sponsor_name: sponsorName.trim() || undefined,
      cro_name: croName.trim() || undefined,
      currency,
      subject_profile: population === 'auto' ? undefined : population,
    }
    if (population === 'patient') {
      if (indication.trim()) payload.indication = indication.trim()
      if (therapeuticArea) payload.therapeutic_area = therapeuticArea
      const sites = numOrUndefined(nSitesSelected)
      if (sites !== undefined) payload.n_sites_selected = sites
      const countries = numOrUndefined(nCountries)
      if (countries !== undefined) payload.n_countries = countries
      const enrolled = numOrUndefined(nEnrolledPatients)
      if (enrolled !== undefined) payload.n_enrolled = enrolled
      if (geographyProfile) payload.geography_profile = geographyProfile
    }
    try {
      const budget = await api.createBudget(payload)
      onCreated(budget)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to create budget')
      setSubmitting(false)
    }
  }

  const namePlaceholder = trimmedDrug ? `Auto: "${trimmedDrug} study budget"` : 'Auto: "<drug> study budget"'

  const spinnerCopy = population === 'patient' || preview?.subject_profile === 'patient'
    ? 'Seeding 22 categories, pricing every line and citing the researched rates…'
    : 'Seeding 17 categories, pricing every line from the rate card…'

  return (
    <main className="app-canvas min-h-[calc(100vh-57px)]">
      <div className="max-w-3xl mx-auto px-5 py-10 space-y-5 fade-up">
        <button onClick={onCancel} className="btn-ghost" disabled={submitting}>
          <LeftArrowIcon />
          All budgets
        </button>

        <div>
          <h1 className="text-2xl font-extrabold text-slate-900 tracking-tight">New Budget</h1>
          <p className="text-sm text-slate-500 mt-1">
            Enter a drug name. We build the full CRO budget — 17 categories for a healthy-volunteer study, 22 for a
            patient study — priced end to end, in one step.
          </p>
        </div>

        {error && (
          <div className="rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">{error}</div>
        )}

        {submitting ? (
          <div className="card p-10 text-center">
            <Spinner size={28} />
            <h2 className="text-lg font-bold text-slate-900 mt-4">
              Building your {trimmedDrug || 'study'} budget
            </h2>
            <p className="text-sm text-slate-500 mt-1.5">{spinnerCopy}</p>
          </div>
        ) : (
          <>
            <div className="card p-6 space-y-4">
              <div>
                <label className="block text-xs font-semibold text-slate-600 mb-1.5">Drug (INN)</label>
                <input
                  type="text"
                  autoFocus
                  value={drugInn}
                  onChange={e => setDrugInn(e.target.value)}
                  onKeyDown={e => { if (e.key === 'Enter') handleSubmit() }}
                  placeholder="e.g. abatacept"
                  className="input-base text-lg font-semibold"
                />
                <p className="text-[11px] text-slate-400 mt-1.5">
                  That&apos;s all we need — everything else is estimated and stays editable.
                </p>
              </div>

              {/* Population — a three-state segmented control, defaulting to
                  Auto-detect. Separate from the (unchanged) study-type gate
                  for biologics below. */}
              <div>
                <label className="block text-xs font-semibold text-slate-600 mb-1.5">Population</label>
                <div className="inline-flex rounded-xl border border-slate-200 p-0.5 flex-wrap">
                  {POPULATION_OPTIONS.map(opt => (
                    <button
                      key={opt.id}
                      type="button"
                      onClick={() => { setPopulation(opt.id); if (opt.id !== 'patient') setShowPatientDetails(false) }}
                      className={`text-xs font-semibold px-3 py-1.5 rounded-lg cursor-pointer transition-colors ${
                        population === opt.id ? 'bg-cyan-500/15 text-cyan-700' : 'text-slate-500 hover:bg-slate-50'
                      }`}
                    >
                      {opt.label}
                    </button>
                  ))}
                </div>

                {(previewLoading || preview) && trimmedDrug && (
                  <p className="flex items-start gap-1.5 text-[11px] leading-relaxed text-slate-500 mt-2">
                    <InfoIcon />
                    {previewLoading && !preview ? (
                      <span className="text-slate-400">Checking {trimmedDrug}&apos;s safety profile…</span>
                    ) : preview ? (
                      <span>{previewHint(trimmedDrug, preview)}</span>
                    ) : null}
                  </p>
                )}
              </div>

              {population === 'patient' && (
                <div className="rounded-xl border border-slate-200 bg-slate-50/60 px-4 py-3.5">
                  <button
                    type="button"
                    onClick={() => setShowPatientDetails(s => !s)}
                    className="w-full flex items-center justify-between gap-2 text-left cursor-pointer"
                  >
                    <div>
                      <p className="text-xs font-semibold text-slate-700">Patient study details</p>
                      <p className="text-[11px] text-slate-400 mt-0.5">
                        Optional — every field falls back to the profile default.
                      </p>
                    </div>
                    <ChevronIcon open={showPatientDetails} />
                  </button>

                  {showPatientDetails && (
                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-3.5 mt-3.5 pt-3.5 border-t border-slate-200">
                      <div className="sm:col-span-2">
                        <label className="block text-[11px] font-semibold text-slate-600 mb-1.5">Indication</label>
                        <input
                          type="text"
                          value={indication}
                          onChange={e => setIndication(e.target.value)}
                          placeholder="e.g. unresectable or metastatic melanoma"
                          className="input-base"
                        />
                      </div>
                      <div>
                        <label className="block text-[11px] font-semibold text-slate-600 mb-1.5">Therapeutic area</label>
                        <select
                          value={therapeuticArea}
                          onChange={e => setTherapeuticArea(e.target.value as TherapeuticAreaChoice)}
                          className="input-base cursor-pointer"
                        >
                          <option value="">Auto-detect</option>
                          <option value="oncology">Oncology</option>
                          <option value="other">Other</option>
                        </select>
                      </div>
                      <div>
                        <label className="block text-[11px] font-semibold text-slate-600 mb-1.5">Geography</label>
                        <select
                          value={geographyProfile}
                          onChange={e => setGeographyProfile(e.target.value as BudgetGeographyProfile | '')}
                          className="input-base cursor-pointer"
                        >
                          <option value="">Auto-detect</option>
                          {GEOGRAPHY_OPTIONS.map(g => <option key={g} value={g}>{GEOGRAPHY_LABELS[g]}</option>)}
                        </select>
                      </div>
                      <div>
                        <label className="block text-[11px] font-semibold text-slate-600 mb-1.5">Sites (activated)</label>
                        <input
                          value={nSitesSelected}
                          onChange={e => setNSitesSelected(e.target.value)}
                          placeholder="40"
                          className="input-base tabular-nums"
                        />
                      </div>
                      <div>
                        <label className="block text-[11px] font-semibold text-slate-600 mb-1.5">Countries</label>
                        <input
                          value={nCountries}
                          onChange={e => setNCountries(e.target.value)}
                          placeholder="7"
                          className="input-base tabular-nums"
                        />
                      </div>
                      <div>
                        <label className="block text-[11px] font-semibold text-slate-600 mb-1.5">Patients enrolled</label>
                        <input
                          value={nEnrolledPatients}
                          onChange={e => setNEnrolledPatients(e.target.value)}
                          placeholder="128"
                          className="input-base tabular-nums"
                        />
                      </div>
                    </div>
                  )}
                </div>
              )}

              <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                <div>
                  <label className="block text-xs font-semibold text-slate-600 mb-1.5">
                    Study type <span className="font-normal text-slate-400">Optional</span>
                  </label>
                  <select
                    value={studyType}
                    onChange={e => setStudyType(e.target.value as StudyType | '')}
                    className="input-base cursor-pointer"
                  >
                    <option value="">Auto-detect</option>
                    {STUDY_TYPES.map(t => <option key={t} value={t}>{STUDY_LABELS[t]}</option>)}
                  </select>
                </div>
                <div>
                  <label className="block text-xs font-semibold text-slate-600 mb-1.5">
                    Currency <span className="font-normal text-slate-400">Optional</span>
                  </label>
                  <select value={currency} onChange={e => setCurrency(e.target.value)} className="input-base cursor-pointer">
                    {CURRENCIES.map(c => <option key={c} value={c}>{c}</option>)}
                  </select>
                </div>
              </div>
            </div>

            <div className="rounded-2xl border border-dashed border-slate-300 bg-white/60 px-5 py-4">
              {!showProtocol ? (
                <div className="flex items-center justify-between gap-3 flex-wrap">
                  <div>
                    <p className="text-sm font-semibold text-slate-700">Link one of your protocols</p>
                    {protocolId ? (
                      <button
                        type="button"
                        onClick={clearProtocol}
                        className="inline-flex items-center gap-1.5 mt-1 text-[11px] font-bold text-cyan-700 bg-cyan-50 border border-cyan-200 rounded-md px-2 py-0.5 cursor-pointer hover:bg-cyan-100"
                      >
                        Linked: {protocolLabel}
                        <span aria-hidden>×</span>
                      </button>
                    ) : (
                      <p className="text-xs text-slate-500 mt-0.5">
                        Optional. Swaps our estimated subject counts and visit schedule for the real ones.
                      </p>
                    )}
                  </div>
                  <button type="button" onClick={() => setShowProtocol(true)} className="btn-ghost text-xs py-1.5 px-3">
                    <LinkIcon />
                    Link a protocol
                  </button>
                </div>
              ) : (
                <div className="space-y-2.5">
                  <div className="flex items-center justify-between gap-3">
                    <p className="text-sm font-semibold text-slate-700">Link one of your protocols</p>
                    <button
                      type="button"
                      onClick={clearProtocol}
                      className="text-xs font-semibold text-slate-500 hover:text-slate-700 cursor-pointer"
                    >
                      {protocolId ? 'Clear' : 'Cancel'}
                    </button>
                  </div>
                  <BudgetProtocolPicker value={protocolId} onChange={handleProtocolChange} disabled={submitting} />
                </div>
              )}
            </div>

            <div>
              <button
                type="button"
                onClick={() => setShowDetails(d => !d)}
                className="btn-ghost text-xs"
              >
                <ChevronIcon open={showDetails} />
                {showDetails ? 'Hide' : 'Add'} name, sponsor and CRO
              </button>
            </div>

            {showDetails && (
              <div className="card p-6 space-y-4">
                <div>
                  <label className="block text-xs font-semibold text-slate-600 mb-1.5">Name</label>
                  <input
                    type="text"
                    value={name}
                    onChange={e => setName(e.target.value)}
                    placeholder={namePlaceholder}
                    className="input-base"
                  />
                </div>
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                  <div>
                    <label className="block text-xs font-semibold text-slate-600 mb-1.5">Sponsor</label>
                    <input
                      type="text"
                      value={sponsorName}
                      onChange={e => setSponsorName(e.target.value)}
                      placeholder="Optional"
                      className="input-base"
                    />
                  </div>
                  <div>
                    <label className="block text-xs font-semibold text-slate-600 mb-1.5">CRO</label>
                    <input
                      type="text"
                      value={croName}
                      onChange={e => setCroName(e.target.value)}
                      placeholder="Optional"
                      className="input-base"
                    />
                  </div>
                </div>
              </div>
            )}

            <div className="flex items-center justify-end gap-3">
              <button onClick={onCancel} className="btn-ghost" disabled={submitting}>Cancel</button>
              <button onClick={handleSubmit} disabled={!canSubmit} className="btn-primary">
                {submitting ? 'Building your budget…' : 'Generate budget'}
              </button>
            </div>
          </>
        )}
      </div>
    </main>
  )
}
