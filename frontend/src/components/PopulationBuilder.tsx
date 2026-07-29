import { useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api/client'
import type {
  DdiRow,
  DdiScreeningResult,
  Drug,
  HepaticStatus,
  PopulationConfig,
  RenalStatus,
  SexType,
  StudyType,
  SubjectType,
} from '../types'

interface Props {
  value: PopulationConfig
  onChange: (config: PopulationConfig) => void
  /** Drives the drug-specific interaction screening. Null before step 1 is done. */
  drug?: Drug | null
  studyType?: StudyType | null
}

/* Drug-AGNOSTIC exclusion criteria only.
 *
 * The list used to carry "CYP3A4 inhibitors or inducers within 14 days" and
 * "Grapefruit or grapefruit juice within 7 days" for EVERY drug, whether or not
 * the product is a CYP3A4 substrate — a pathway claim the protocol could not
 * defend. Pathway-specific comedication criteria now come from the backend's
 * interaction screening, which reads the drug's real DailyMed label; the
 * frontend never assumes a pathway of its own. */
const GENERIC_EXCLUSIONS = [
  'Tobacco or nicotine use within 6 months',
  'Alcohol use > 14 units per week',
  'Prescription medications within 14 days',
  'Blood donation within 3 months',
  'Heavy exercise within 48 hours of dosing',
  'History of drug or alcohol abuse',
]

/** Display names for the pathway keys the screening engine emits. */
const PATHWAY_LABELS: Record<string, string> = {
  cyp3a4: 'CYP3A4',
  cyp2d6: 'CYP2D6',
  cyp2c19: 'CYP2C19',
  cyp2c9: 'CYP2C9',
  cyp2c8: 'CYP2C8',
  cyp1a2: 'CYP1A2',
  cyp2b6: 'CYP2B6',
  ugt: 'UGT',
  pgp: 'P-gp',
  bcrp: 'BCRP',
  oatp: 'OATP',
  oct_mate: 'OCT/MATE',
}

const RENAL_OPTIONS: Array<{ value: RenalStatus; label: string }> = [
  { value: 'normal', label: 'Normal' },
  { value: 'mild', label: 'Mild (eGFR 60–89)' },
  { value: 'moderate', label: 'Moderate (eGFR 30–59)' },
  { value: 'severe', label: 'Severe (< 30)' },
]

const HEPATIC_OPTIONS: Array<{ value: HepaticStatus; label: string }> = [
  { value: 'normal', label: 'Normal' },
  { value: 'mild', label: 'Mild (Child-Pugh A)' },
  { value: 'moderate', label: 'Moderate (Child-Pugh B)' },
  { value: 'severe', label: 'Severe (Child-Pugh C)' },
]

function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <p className="text-[11px] font-bold text-slate-500 uppercase tracking-widest mb-3">
      {children}
    </p>
  )
}

function FieldLabel({ children }: { children: React.ReactNode }) {
  return (
    <label className="block text-xs font-semibold text-slate-600 mb-2">{children}</label>
  )
}

function ToggleBtn({
  active,
  onClick,
  children,
}: {
  active: boolean
  onClick: () => void
  children: React.ReactNode
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={[
        'px-3.5 py-1.5 text-xs font-semibold rounded-lg border transition-all duration-150 cursor-pointer',
        active
          ? 'bg-cyan-600 text-white border-cyan-600 shadow-sm'
          : 'bg-white text-slate-600 border-slate-200 hover:bg-slate-50 hover:border-slate-300',
      ].join(' ')}
    >
      {children}
    </button>
  )
}

function NumberInput({
  value,
  min,
  max,
  onChange,
  unit,
}: {
  value: number
  min: number
  max: number
  onChange: (n: number) => void
  unit?: string
}) {
  return (
    <div className="relative">
      <input
        type="number"
        value={value}
        min={min}
        max={max}
        onChange={e => {
          const n = parseInt(e.target.value, 10)
          if (!isNaN(n)) onChange(Math.min(max, Math.max(min, n)))
        }}
        className="w-16 px-2.5 py-1.5 border border-slate-200 rounded-lg text-sm text-slate-900 text-center font-semibold focus:outline-none focus:ring-2 focus:ring-cyan-500/40 focus:border-cyan-400 transition-all"
      />
      {unit && <span className="ml-1.5 text-xs text-slate-400">{unit}</span>}
    </div>
  )
}

function Divider() {
  return <div className="border-t border-slate-100 my-6" />
}

function CheckMark() {
  return (
    <svg width="10" height="10" viewBox="0 0 10 10" fill="none" className="flex-shrink-0">
      <path d="M2 5l2 2 4-4" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}

/* ── Interaction-screening helpers ─────────────────────────────────────────
 * A screening row becomes ONE exclusion-criterion string, because that is what
 * population_config.exclusion_presets carries into the protocol. The engine's
 * own sentence is used verbatim when it supplies one, so the wording the
 * protocol states is the wording the engine wrote (including its washout).   */

function formatDays(days: number): string {
  const rounded = Number.isInteger(days) ? days : Number(days.toFixed(1))
  return `${rounded} day${rounded === 1 ? '' : 's'}`
}

function criterionText(row: DdiRow): string {
  if (row.criterion && row.criterion.trim()) return row.criterion.trim()

  const examples = (row.examples ?? []).filter(Boolean).slice(0, 3)
  const head = examples.length ? `${row.label} (e.g. ${examples.join(', ')})` : row.label

  if (row.history_exclusion) return `${head}: excluded on history (cannot be washed out)`
  if (typeof row.washout_days === 'number' && row.washout_days > 0) {
    return `${head} within ${formatDays(row.washout_days)} before first dose`
  }
  if (row.conditions) return `${head}: ${row.conditions}`
  return head
}

/** Everything the engine knows about a row, shown on hover — the provenance a
 *  reviewer needs to accept or reject the criterion. */
function criterionTooltip(row: DdiRow): string {
  const lines: string[] = []
  if (row.pathway) lines.push(`Pathway: ${PATHWAY_LABELS[row.pathway] ?? row.pathway}`)
  if (row.rationale) lines.push(row.rationale)
  if (row.washout_basis) lines.push(`Washout basis: ${row.washout_basis}`)
  if (row.conditions) lines.push(`Conditions: ${row.conditions}`)
  if (row.escalated_by_nti) lines.push('Escalated because the drug is narrow-therapeutic-index.')
  if (row.assumption) lines.push(`Assumption: ${row.assumption}`)
  const quote = row.evidence?.quote
  if (quote) lines.push(`Label evidence: "${quote}"`)
  return lines.join('\n')
}

export default function PopulationBuilder({ value, onChange, drug, studyType }: Props) {
  const set = <K extends keyof PopulationConfig>(key: K, val: PopulationConfig[K]) =>
    onChange({ ...value, [key]: val })

  const toggleExclusion = (preset: string) => {
    const has = value.exclusion_presets.includes(preset)
    set(
      'exclusion_presets',
      has ? value.exclusion_presets.filter(p => p !== preset) : [...value.exclusion_presets, preset],
    )
  }

  const impaired = value.renal_status !== 'normal' || value.hepatic_status !== 'normal'

  // ── Drug-specific interaction screening ────────────────────────────────
  const [ddi, setDdi] = useState<DdiScreeningResult | null>(null)
  const [ddiLoading, setDdiLoading] = useState(false)
  const [ddiError, setDdiError] = useState<string | null>(null)
  const [customDraft, setCustomDraft] = useState('')

  // Latest config, read inside the fetch effect without making it a dependency
  // (adding `value` would refire the screening on every keystroke elsewhere).
  const valueRef = useRef(value)
  valueRef.current = value
  const onChangeRef = useRef(onChange)
  onChangeRef.current = onChange

  const drugId = drug?.id ?? null
  const isNti = value.is_nti === true

  useEffect(() => {
    if (!drugId || !studyType) {
      setDdi(null)
      setDdiError(null)
      setDdiLoading(false)
      return
    }
    let cancelled = false
    setDdiLoading(true)
    setDdiError(null)
    api.previewDdi(drugId, studyType, valueRef.current)
      .then(result => {
        if (cancelled) return
        setDdi(result)
        if (!result) return

        // Adopt the engine's PROHIBITED rows as the starting selection, but only
        // while the user hasn't engaged with this drug's list yet. Once any of
        // its rows is selected the choice is theirs and is never overwritten —
        // including on a re-visit to this step.
        const rows = [...(result.prohibited ?? []), ...(result.restricted ?? [])]
        const selected = valueRef.current.exclusion_presets
        const touched = rows.some(r => selected.includes(criterionText(r)))
        if (touched) return
        const additions = (result.prohibited ?? [])
          .map(criterionText)
          .filter(text => text && !selected.includes(text))
        if (additions.length) {
          onChangeRef.current({
            ...valueRef.current,
            exclusion_presets: [...valueRef.current.exclusion_presets, ...additions],
          })
        }
      })
      .catch(e => {
        if (!cancelled) setDdiError(e instanceof Error ? e.message : 'Interaction screening unavailable')
      })
      .finally(() => { if (!cancelled) setDdiLoading(false) })
    return () => { cancelled = true }
  }, [drugId, studyType, isNti])

  const screeningRows = useMemo<DdiRow[]>(() => {
    if (!ddi) return []
    return [...(ddi.prohibited ?? []), ...(ddi.restricted ?? [])]
  }, [ddi])

  const screeningTexts = useMemo(
    () => screeningRows.map(criterionText),
    [screeningRows],
  )

  // Anything selected that neither list offers — a criterion the user typed, or
  // one carried over from a previous drug. It stays visible and removable
  // instead of silently disappearing when the offered list changes.
  const customSelections = useMemo(
    () => value.exclusion_presets.filter(
      p => !GENERIC_EXCLUSIONS.includes(p) && !screeningTexts.includes(p),
    ),
    [value.exclusion_presets, screeningTexts],
  )

  const pathwayKeys = ddi?.pathway_keys ?? []
  const noPathwayResolved = !!ddi && pathwayKeys.length === 0

  const addCustom = () => {
    const text = customDraft.trim()
    if (!text) return
    if (!value.exclusion_presets.includes(text)) {
      set('exclusion_presets', [...value.exclusion_presets, text])
    }
    setCustomDraft('')
  }

  const removeCriterion = (text: string) =>
    set('exclusion_presets', value.exclusion_presets.filter(p => p !== text))

  const cvOverridden = typeof value.cv_intra === 'number' && !isNaN(value.cv_intra)

  return (
    <div>
      {/* Subject Type */}
      <div>
        <SectionLabel>Subject Type</SectionLabel>
        <div className="flex gap-2">
          <ToggleBtn active={value.subject_type === 'healthy'} onClick={() => set('subject_type', 'healthy' as SubjectType)}>
            Healthy Volunteers
          </ToggleBtn>
          <ToggleBtn active={value.subject_type === 'patient'} onClick={() => set('subject_type', 'patient' as SubjectType)}>
            Patients
          </ToggleBtn>
        </div>
        <p className="text-xs text-slate-400 mt-2 leading-relaxed">
          {value.subject_type === 'healthy'
            ? 'Standard for most BE studies. Exclusion criteria must ensure no clinically relevant disease.'
            : 'Used when healthy volunteer studies are inappropriate or infeasible (e.g. narrow therapeutic index drugs).'}
        </p>
      </div>

      <Divider />

      {/* Demographics */}
      <div>
        <SectionLabel>Demographics</SectionLabel>
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-6">
          <div>
            <FieldLabel>Age Range (years)</FieldLabel>
            <div className="flex items-center gap-2">
              <NumberInput value={value.age_min} min={18} max={value.age_max} onChange={n => set('age_min', n)} />
              <span className="text-slate-300 text-sm font-light select-none">—</span>
              <NumberInput value={value.age_max} min={value.age_min} max={80} onChange={n => set('age_max', n)} />
            </div>
          </div>

          <div>
            <FieldLabel>Sex</FieldLabel>
            <div className="flex gap-1.5">
              {(['male', 'female', 'both'] as SexType[]).map(s => (
                <button
                  key={s}
                  type="button"
                  onClick={() => set('sex', s)}
                  className={[
                    'flex-1 px-2 py-1.5 text-xs font-semibold rounded-lg border transition-all duration-150 cursor-pointer whitespace-nowrap',
                    value.sex === s
                      ? 'bg-cyan-600 text-white border-cyan-600 shadow-sm'
                      : 'bg-white text-slate-600 border-slate-200 hover:bg-slate-50 hover:border-slate-300',
                  ].join(' ')}
                >
                  {s === 'both' ? 'Both' : s === 'male' ? 'Male' : 'Female'}
                </button>
              ))}
            </div>
          </div>

          <div>
            <FieldLabel>Weight Range (kg)</FieldLabel>
            <div className="flex items-center gap-2">
              <NumberInput value={value.weight_min} min={40} max={value.weight_max} onChange={n => set('weight_min', n)} />
              <span className="text-slate-300 text-sm font-light select-none">—</span>
              <NumberInput value={value.weight_max} min={value.weight_min} max={150} onChange={n => set('weight_max', n)} />
            </div>
          </div>
        </div>
      </div>

      <Divider />

      {/* Organ function */}
      <div>
        <SectionLabel>Organ Function</SectionLabel>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-6">
          <div>
            <FieldLabel>Renal Status</FieldLabel>
            <div className="flex flex-wrap gap-1.5">
              {RENAL_OPTIONS.map(opt => (
                <ToggleBtn key={opt.value} active={value.renal_status === opt.value} onClick={() => set('renal_status', opt.value)}>
                  {opt.label}
                </ToggleBtn>
              ))}
            </div>
          </div>

          <div>
            <FieldLabel>Hepatic Status</FieldLabel>
            <div className="flex flex-wrap gap-1.5">
              {HEPATIC_OPTIONS.map(opt => (
                <ToggleBtn key={opt.value} active={value.hepatic_status === opt.value} onClick={() => set('hepatic_status', opt.value)}>
                  {opt.label}
                </ToggleBtn>
              ))}
            </div>
          </div>
        </div>

        {impaired && (
          <div className="mt-4 flex items-start gap-2.5 p-3.5 bg-amber-50 border border-amber-200 rounded-xl">
            <svg width="15" height="15" viewBox="0 0 15 15" fill="none" className="flex-shrink-0 mt-0.5">
              <path d="M7.5 1.5L13.5 12H1.5L7.5 1.5z" stroke="#d97706" strokeWidth="1.5" strokeLinejoin="round" />
              <path d="M7.5 5.5v3M7.5 10v.5" stroke="#d97706" strokeWidth="1.5" strokeLinecap="round" />
            </svg>
            <p className="text-xs text-amber-700 leading-relaxed">
              Impaired organ function will be reflected in eligibility criteria and safety monitoring sections. Dosing adjustment language will be included where applicable.
            </p>
          </div>
        )}
      </div>

      <Divider />

      {/* Exclusion criteria */}
      <div>
        <div className="flex items-center justify-between mb-3">
          <SectionLabel>Exclusion Criteria</SectionLabel>
          <span className="text-xs text-slate-400 font-medium">
            {value.exclusion_presets.length} selected
          </span>
        </div>
        <p className="text-xs text-slate-400 mb-4 leading-relaxed">
          Selected criteria are included verbatim in the Subject Selection section.
        </p>

        {/* ── Drug-specific: prohibited / restricted comedications ────────── */}
        <div className="mb-5">
          <div className="flex items-center gap-2 mb-2 flex-wrap">
            <p className="text-[11px] font-bold text-slate-500 uppercase tracking-wide">
              Interaction screening
            </p>
            {drug && (
              <span className="text-[11px] text-slate-400 capitalize">· {drug.inn}</span>
            )}
            {pathwayKeys.length > 0 && pathwayKeys.map(k => (
              <span
                key={k}
                className="text-[10px] font-bold text-violet-700 bg-violet-50 border border-violet-200 px-1.5 py-0.5 rounded-md"
              >
                {PATHWAY_LABELS[k] ?? k.toUpperCase()}
              </span>
            ))}
          </div>

          {!drug || !studyType ? (
            <div className="rounded-lg border border-slate-200 bg-slate-50/70 px-3 py-2.5">
              <p className="text-[11px] text-slate-500 leading-relaxed">
                Select a drug and study type first. Pathway-specific criteria (CYP and
                transporter perpetrators, grapefruit, and their washout windows) are read
                from the product&apos;s own label — they are never assumed.
              </p>
            </div>
          ) : ddiLoading ? (
            <div className="rounded-lg border border-slate-200 bg-slate-50/70 px-3 py-2.5 animate-pulse">
              <p className="text-[11px] text-slate-500">
                Screening the {drug.inn} label for interaction pathways…
              </p>
            </div>
          ) : ddiError || !ddi ? (
            <div className="rounded-lg border border-slate-200 bg-slate-50/70 px-3 py-2.5">
              <p className="text-[11px] text-slate-500 leading-relaxed">
                <span className="font-medium text-slate-600">
                  Drug-specific screening isn&apos;t available in this session.
                </span>{' '}
                Only drug-agnostic criteria are offered here — no pathway is assumed.
                The interaction screening still runs server-side during generation and
                its findings are written into the protocol.
                {ddiError ? ` (${ddiError})` : ''}
              </p>
            </div>
          ) : screeningRows.length === 0 ? (
            <div className="rounded-lg border border-slate-200 bg-slate-50/70 px-3 py-2.5">
              <p className="text-[11px] text-slate-500 leading-relaxed">
                No comedication restriction was derived from the {drug.inn} label.
              </p>
            </div>
          ) : (
            <>
              <div className="flex flex-wrap gap-2">
                {screeningRows.map((row, idx) => {
                  const text = screeningTexts[idx]
                  const active = value.exclusion_presets.includes(text)
                  const prohibited = row.status === 'prohibited'
                  return (
                    <button
                      key={`${row.key}-${idx}`}
                      type="button"
                      title={criterionTooltip(row)}
                      onClick={() => toggleExclusion(text)}
                      className={[
                        'inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium border transition-all duration-150 cursor-pointer text-left',
                        active
                          ? 'bg-cyan-50 border-cyan-300 text-cyan-700'
                          : 'bg-white border-slate-200 text-slate-500 hover:border-slate-300 hover:text-slate-700',
                      ].join(' ')}
                    >
                      {active && <CheckMark />}
                      <span
                        className={[
                          'w-1.5 h-1.5 rounded-full flex-shrink-0',
                          prohibited ? 'bg-red-400' : 'bg-amber-400',
                        ].join(' ')}
                        aria-hidden="true"
                      />
                      {text}
                    </button>
                  )
                })}
              </div>
              <p className="text-[11px] text-slate-400 mt-2 leading-relaxed">
                <span className="inline-block w-1.5 h-1.5 rounded-full bg-red-400 align-middle mr-1" />
                <span>prohibited</span>
                <span className="mx-2">·</span>
                <span className="inline-block w-1.5 h-1.5 rounded-full bg-amber-400 align-middle mr-1" />
                <span>restricted</span>
                {' · '}
                derived from the DailyMed label; hover a criterion for its rationale,
                washout basis and the label text it was read from.
              </p>
            </>
          )}

          {noPathwayResolved && (
            <div className="mt-3 flex items-start gap-2.5 p-3 bg-amber-50 border border-amber-200 rounded-xl">
              <svg width="15" height="15" viewBox="0 0 15 15" fill="none" className="flex-shrink-0 mt-0.5">
                <path d="M7.5 1.5L13.5 12H1.5L7.5 1.5z" stroke="#d97706" strokeWidth="1.5" strokeLinejoin="round" />
                <path d="M7.5 5.5v3M7.5 10v.5" stroke="#d97706" strokeWidth="1.5" strokeLinecap="round" />
              </svg>
              <p className="text-[11px] text-amber-700 leading-relaxed">
                No metabolic or transporter pathway could be identified from the label, so a
                conservative pan-pathway restriction is applied at the longest window. The
                protocol discloses that this is a fallback rather than a label finding.
              </p>
            </div>
          )}

          {(ddi?.history_exclusion_agents?.length ?? 0) > 0 && (
            <p className="text-[11px] text-slate-400 mt-2 leading-relaxed">
              Excluded on history (no washout possible):{' '}
              <span className="text-slate-500">{ddi?.history_exclusion_agents?.join(', ')}</span>
            </p>
          )}
        </div>

        {/* ── Drug-agnostic criteria ─────────────────────────────────────── */}
        <p className="text-[11px] font-bold text-slate-500 uppercase tracking-wide mb-2">
          General eligibility
        </p>
        <div className="flex flex-wrap gap-2">
          {GENERIC_EXCLUSIONS.map(preset => {
            const active = value.exclusion_presets.includes(preset)
            return (
              <button
                key={preset}
                type="button"
                onClick={() => toggleExclusion(preset)}
                className={[
                  'inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium border transition-all duration-150 cursor-pointer',
                  active
                    ? 'bg-cyan-50 border-cyan-300 text-cyan-700'
                    : 'bg-white border-slate-200 text-slate-500 hover:border-slate-300 hover:text-slate-700',
                ].join(' ')}
              >
                {active && <CheckMark />}
                {preset}
              </button>
            )
          })}
        </div>

        {/* ── The user's own criteria ────────────────────────────────────── */}
        {customSelections.length > 0 && (
          <>
            <p className="text-[11px] font-bold text-slate-500 uppercase tracking-wide mt-5 mb-2">
              Your criteria
            </p>
            <div className="flex flex-wrap gap-2">
              {customSelections.map(text => (
                <span
                  key={text}
                  className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium border bg-cyan-50 border-cyan-300 text-cyan-700"
                >
                  <CheckMark />
                  {text}
                  <button
                    type="button"
                    onClick={() => removeCriterion(text)}
                    aria-label={`Remove criterion: ${text}`}
                    title="Remove"
                    className="ml-0.5 text-cyan-500 hover:text-cyan-800 cursor-pointer leading-none"
                  >
                    <svg width="9" height="9" viewBox="0 0 9 9" fill="none">
                      <path d="M1.5 1.5l6 6M7.5 1.5l-6 6" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
                    </svg>
                  </button>
                </span>
              ))}
            </div>
          </>
        )}

        <div className="mt-4 flex gap-2">
          <input
            type="text"
            value={customDraft}
            placeholder="Add your own exclusion criterion"
            onChange={e => setCustomDraft(e.target.value)}
            onKeyDown={e => {
              if (e.key === 'Enter') {
                e.preventDefault()
                addCustom()
              }
            }}
            className="flex-1 px-3 py-2 border border-slate-200 rounded-lg text-sm text-slate-900 placeholder-slate-400 focus:outline-none focus:ring-2 focus:ring-cyan-500/40 focus:border-cyan-400 transition-all"
          />
          <button
            type="button"
            onClick={addCustom}
            disabled={!customDraft.trim()}
            className="px-4 py-2 rounded-lg text-xs font-semibold border border-slate-200 text-slate-600 bg-white hover:bg-slate-50 hover:border-slate-300 disabled:opacity-50 disabled:cursor-not-allowed transition-all cursor-pointer"
          >
            Add
          </button>
        </div>
      </div>

      <Divider />

      {/* Study design parameters (optional) */}
      <div>
        <SectionLabel>Study Design Parameters — Optional</SectionLabel>
        <p className="text-xs text-slate-400 mb-4 leading-relaxed">
          Supplying these refines the auto-computed sample size and design. Left blank,
          the within-subject CV is taken from the literature (or a flagged assumption) and
          the Reference product / strength are written as placeholders.
        </p>

        {/* Within-subject CV is OPTIONAL and the blank state is the safer path:
            the powering CV is then retrieved from the published literature
            (highest observed CV for Cmax or AUC across healthy-volunteer studies
            — the conservative regulatory convention) and the supporting trials
            are tabulated in the SAP. A typed value OVERRIDES that sourced,
            citable figure, so the consequence is stated in the UI rather than
            left for the reader of the protocol to discover. */}
        <div
          className={[
            'mb-4 rounded-lg border px-3 py-2.5',
            cvOverridden ? 'border-amber-200 bg-amber-50' : 'border-slate-200 bg-slate-50/70',
          ].join(' ')}
        >
          {cvOverridden ? (
            <p className="text-[11px] text-amber-700 leading-relaxed">
              <span className="font-semibold">
                Investigator-specified CV overrides the literature value.
              </span>{' '}
              The study will be powered on {value.cv_intra}% instead of the highest
              within-subject CV reported for Cmax or AUC in the healthy-volunteer
              literature, and the protocol will declare that figure as
              investigator-specified rather than literature-sourced — the SAP&apos;s
              supporting-studies table does not back it. Clear the field to return to the
              sourced value.
            </p>
          ) : (
            <p className="text-[11px] text-slate-500 leading-relaxed">
              <span className="font-medium text-slate-600">
                Within-subject CV is retrieved automatically.
              </span>{' '}
              Left blank (recommended), the sample size is powered on the highest
              within-subject CV reported for Cmax or AUC in the healthy-volunteer
              literature, with the supporting studies tabulated in the Statistical
              Analysis Plan. Enter a value only to override that sourced figure.
            </p>
          )}
        </div>

        <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-6 gap-y-4">
          <div>
            <FieldLabel>Within-subject CV (%) — optional</FieldLabel>
            <input
              type="number"
              min={1}
              max={120}
              step={0.5}
              value={value.cv_intra ?? ''}
              placeholder="Blank = use literature value"
              onChange={e => {
                const n = parseFloat(e.target.value)
                set('cv_intra', e.target.value === '' || isNaN(n) ? undefined : n)
              }}
              className={[
                'w-full px-3 py-2 border rounded-lg text-sm text-slate-900 placeholder-slate-400 focus:outline-none focus:ring-2 transition-all',
                cvOverridden
                  ? 'border-amber-300 bg-amber-50/40 focus:ring-amber-400/40 focus:border-amber-400'
                  : 'border-slate-200 focus:ring-cyan-500/40 focus:border-cyan-400',
              ].join(' ')}
            />
            {cvOverridden ? (
              <button
                type="button"
                onClick={() => set('cv_intra', undefined)}
                className="text-[11px] text-cyan-700 hover:text-cyan-800 underline underline-offset-2 mt-1 cursor-pointer"
              >
                Clear — use the literature-sourced CV
              </button>
            ) : (
              <p className="text-[11px] text-slate-400 mt-1">Drives the BE sample-size calculation.</p>
            )}
          </div>

          <div>
            <FieldLabel>Dose Strength</FieldLabel>
            <input
              type="text"
              value={value.dose_strength ?? ''}
              placeholder="e.g. 400 mg film-coated tablet"
              onChange={e => set('dose_strength', e.target.value || undefined)}
              className="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm text-slate-900 placeholder-slate-400 focus:outline-none focus:ring-2 focus:ring-cyan-500/40 focus:border-cyan-400 transition-all"
            />
          </div>

          <div className="sm:col-span-2">
            <FieldLabel>Reference Listed Drug (RLD)</FieldLabel>
            <input
              type="text"
              value={value.reference_product ?? ''}
              placeholder="e.g. Brand® 400 mg (Manufacturer)"
              onChange={e => set('reference_product', e.target.value || undefined)}
              className="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm text-slate-900 placeholder-slate-400 focus:outline-none focus:ring-2 focus:ring-cyan-500/40 focus:border-cyan-400 transition-all"
            />
          </div>
        </div>

        <label className="mt-4 flex items-start gap-2.5 cursor-pointer select-none">
          <input
            type="checkbox"
            checked={value.is_nti === true}
            onChange={e => set('is_nti', e.target.checked ? true : undefined)}
            className="mt-0.5 w-4 h-4 rounded border-slate-300 text-cyan-600 focus:ring-cyan-500/40 cursor-pointer"
          />
          <span className="text-xs text-slate-600 leading-relaxed">
            <span className="font-semibold">Narrow Therapeutic Index (NTI) drug</span> — applies
            tightened 90.00–111.11% acceptance limits and a fully replicated design with
            reference scaling (e.g. warfarin, tacrolimus, levothyroxine).
          </span>
        </label>
      </div>
    </div>
  )
}
