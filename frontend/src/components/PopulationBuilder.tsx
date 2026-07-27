import type {
  HepaticStatus,
  PopulationConfig,
  RenalStatus,
  SexType,
  SubjectType,
} from '../types'

interface Props {
  value: PopulationConfig
  onChange: (config: PopulationConfig) => void
}

const EXCLUSION_PRESETS = [
  'Tobacco or nicotine use within 6 months',
  'Alcohol use > 14 units per week',
  'CYP3A4 inhibitors or inducers within 14 days',
  'Prescription medications within 14 days',
  'Blood donation within 3 months',
  'Grapefruit or grapefruit juice within 7 days',
  'Heavy exercise within 48 hours of dosing',
  'History of drug or alcohol abuse',
]

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

export default function PopulationBuilder({ value, onChange }: Props) {
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

      {/* Exclusion presets */}
      <div>
        <div className="flex items-center justify-between mb-3">
          <SectionLabel>Exclusion Criteria Presets</SectionLabel>
          <span className="text-xs text-slate-400 font-medium">
            {value.exclusion_presets.length} selected
          </span>
        </div>
        <p className="text-xs text-slate-400 mb-3 leading-relaxed">
          Selected presets are included verbatim in the Subject Selection section.
        </p>
        <div className="flex flex-wrap gap-2">
          {EXCLUSION_PRESETS.map(preset => {
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
                {active && (
                  <svg width="10" height="10" viewBox="0 0 10 10" fill="none">
                    <path d="M2 5l2 2 4-4" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" />
                  </svg>
                )}
                {preset}
              </button>
            )
          })}
        </div>
      </div>

      <Divider />

      {/* Study design parameters (optional) */}
      <div>
        <SectionLabel>Study Design Parameters — Optional</SectionLabel>
        <p className="text-xs text-slate-400 mb-4 leading-relaxed">
          Supplying these refines the auto-computed sample size and design. Left blank,
          the Reference product / strength are written as placeholders.
        </p>

        {/* Within-subject CV is deliberately NOT an input: it is retrieved
            automatically from the published literature (highest observed CV for
            Cmax or AUC across healthy-volunteer studies — the conservative
            regulatory convention) and the supporting trials are tabulated in the
            SAP. A hand-typed CV silently overrode that sourced, citable value. */}
        <div className="mb-4 rounded-lg border border-slate-200 bg-slate-50/70 px-3 py-2.5">
          <p className="text-[11px] text-slate-500 leading-relaxed">
            <span className="font-medium text-slate-600">Within-subject CV is retrieved automatically.</span>{' '}
            The sample size is powered on the highest within-subject CV reported for Cmax or
            AUC in the healthy-volunteer literature, with the supporting studies tabulated in
            the Statistical Analysis Plan.
          </p>
        </div>

        <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-6 gap-y-4">
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
