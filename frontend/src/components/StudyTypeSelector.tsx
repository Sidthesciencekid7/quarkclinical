import type { Drug, DrugModality, StudyType } from '../types'

interface Props {
  value: StudyType | null
  onChange: (type: StudyType) => void
  drug?: Drug | null
}

// Mirrors services/design_decisions.py::STUDY_TYPES_BY_MODALITY on the backend
// (which is the authoritative, enforced source of truth — this only drives
// the UI so the user isn't allowed to pick a combination the server would
// reject with 422).
const STUDY_TYPES_BY_MODALITY: Record<'biologic' | 'small_molecule', StudyType[]> = {
  biologic: ['biosimilar_pk'],
  small_molecule: ['BE', 'food_effect'],
}

export function allowedStudyTypes(modality: DrugModality | undefined): StudyType[] | null {
  if (modality === 'biologic' || modality === 'small_molecule') {
    return STUDY_TYPES_BY_MODALITY[modality]
  }
  return null // unknown / unclassified — don't restrict
}

const TYPES: Array<{
  value: StudyType
  label: string
  subtitle: string
  reg: string
  description: string
  design: string
  badge?: string
  accent: string
  accentBg: string
  icon: React.ReactNode
}> = [
  {
    value: 'BE',
    label: 'Bioequivalence',
    subtitle: 'Small Molecule · Chemical Drug',
    reg: '21 CFR Part 320 · FDA BE Guidance 2014 · ICH E6(R2)',
    description:
      'Generic or new formulations of small molecule chemical drugs. Demonstrates PK equivalence to the Reference Listed Drug. ' +
      'Acceptance: 90% CI for Cmax and AUC₀₋ₜ within 80.00–125.00%. Bioanalytical: LC-MS/MS.',
    design: '2-period crossover · TR/RT sequences · Single dose · Fasting or fed',
    badge: 'Chemical',
    accent: 'border-cyan-400 ring-1 ring-cyan-200',
    accentBg: 'bg-cyan-600',
    icon: (
      <svg width="20" height="20" viewBox="0 0 20 20" fill="none">
        <rect x="2" y="8" width="16" height="4.5" rx="2.25" stroke="currentColor" strokeWidth="1.5" />
        <path d="M8 10.25h4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
        <path d="M10 8V5.5M10 12.5V15" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
      </svg>
    ),
  },
  {
    value: 'biosimilar_pk',
    label: 'Biosimilar PK Similarity',
    subtitle: 'Biologic · Biosimilar Product',
    reg: 'FDA 351(k) Pathway · FDA Biosimilar Guidance 2019 · ICH E6(R2)',
    description:
      'Biological products (proteins, mAbs, peptides). Establishes PK similarity between biosimilar and reference biological. ' +
      'Mandatory immunogenicity (ADA + NAb) at baseline, Week 4, 8, 12, 24. Bioanalytical: ELISA/ECL LBA.',
    design: 'Parallel group · Single dose · Immunogenicity through Week 24',
    badge: 'Biologic',
    accent: 'border-violet-400 ring-1 ring-violet-200',
    accentBg: 'bg-violet-600',
    icon: (
      <svg width="20" height="20" viewBox="0 0 20 20" fill="none">
        <circle cx="10" cy="10" r="3" stroke="currentColor" strokeWidth="1.5" />
        <circle cx="4" cy="6" r="2" stroke="currentColor" strokeWidth="1.25" />
        <circle cx="16" cy="6" r="2" stroke="currentColor" strokeWidth="1.25" />
        <circle cx="4" cy="14" r="2" stroke="currentColor" strokeWidth="1.25" />
        <circle cx="16" cy="14" r="2" stroke="currentColor" strokeWidth="1.25" />
        <path d="M6 7l2.5 2.5M14 7l-2.5 2.5M6 13l2.5-2.5M14 13l-2.5-2.5" stroke="currentColor" strokeWidth="1.1" strokeLinecap="round" />
      </svg>
    ),
  },
  {
    value: 'food_effect',
    label: 'Food Effect',
    subtitle: 'Fed vs. Fasted BA/BE',
    reg: 'FDA Food-Effect BA/BE Guidance 2002 · 21 CFR 320.25',
    description:
      'Assesses the impact of a high-fat, high-calorie meal on drug absorption and bioavailability. ' +
      'Required for immediate-release oral dosage forms before NDA/ANDA submission.',
    design: '2-period crossover · Single dose · Fed vs. fasted',
    badge: undefined,
    accent: 'border-amber-400 ring-1 ring-amber-200',
    accentBg: 'bg-amber-500',
    icon: (
      <svg width="20" height="20" viewBox="0 0 20 20" fill="none">
        <path d="M4 8c0-3.31 2.69-6 6-6s6 2.69 6 6" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
        <path d="M3 8h14l-1.5 8H4.5L3 8z" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
      </svg>
    ),
  },
]

const BADGE_STYLES: Record<string, { sel: string; unsel: string }> = {
  Chemical: {
    sel: 'bg-cyan-100 text-cyan-700 border-cyan-200',
    unsel: 'bg-cyan-50 text-cyan-500 border-cyan-100',
  },
  Biologic: {
    sel: 'bg-violet-100 text-violet-700 border-violet-200',
    unsel: 'bg-violet-50 text-violet-500 border-violet-100',
  },
}

export default function StudyTypeSelector({ value, onChange, drug }: Props) {
  const allowed = allowedStudyTypes(drug?.modality)

  return (
    <div className="space-y-3">
      {allowed && (
        <p className="text-xs font-medium text-slate-500 bg-slate-50 border border-slate-200 rounded-lg px-3 py-2">
          {drug?.modality === 'biologic'
            ? `${drug?.inn} is a biologic — only the Biosimilar PK Similarity study type applies.`
            : `${drug?.inn} is a small-molecule product — the Biosimilar PK Similarity study type doesn't apply.`}
        </p>
      )}
      {TYPES.map(type => {
        const selected = value === type.value
        const isAllowed = !allowed || allowed.includes(type.value)
        return (
          <button
            key={type.value}
            type="button"
            disabled={!isAllowed}
            onClick={() => isAllowed && onChange(type.value)}
            title={isAllowed ? undefined : `Not applicable to ${drug?.inn} (${drug?.modality === 'biologic' ? 'biologic' : 'small molecule'})`}
            className={[
              'w-full text-left rounded-xl border-2 transition-all duration-200 relative overflow-hidden group',
              !isAllowed ? 'opacity-40 cursor-not-allowed grayscale' : 'cursor-pointer',
              selected
                ? `${type.accent} bg-white shadow-sm`
                : 'border-slate-200 bg-white hover:border-slate-300 hover:shadow-sm',
            ].join(' ')}
          >
            {/* Left accent bar */}
            <div
              className={[
                'absolute left-0 inset-y-0 w-1 transition-all duration-200',
                selected ? type.accentBg : 'bg-slate-200 group-hover:bg-slate-300',
              ].join(' ')}
            />

            <div className="pl-5 pr-5 py-4">
              <div className="flex items-start gap-3.5">
                {/* Radio */}
                <div
                  className={[
                    'mt-0.5 w-4.5 h-4.5 rounded-full border-2 flex-shrink-0 flex items-center justify-center transition-colors',
                    selected ? 'border-current' : 'border-slate-300',
                    selected && type.value === 'BE' ? 'border-cyan-600' : '',
                    selected && type.value === 'biosimilar_pk' ? 'border-violet-600' : '',
                    selected && type.value === 'food_effect' ? 'border-amber-500' : '',
                  ].join(' ')}
                  style={{ width: 18, height: 18 }}
                >
                  {selected && (
                    <div className={[
                      'w-2.5 h-2.5 rounded-full',
                      type.value === 'BE' ? 'bg-cyan-600' : '',
                      type.value === 'biosimilar_pk' ? 'bg-violet-600' : '',
                      type.value === 'food_effect' ? 'bg-amber-500' : '',
                    ].join(' ')} style={{ width: 10, height: 10 }} />
                  )}
                </div>

                {/* Icon */}
                <div className={[
                  'w-9 h-9 rounded-xl flex items-center justify-center flex-shrink-0 transition-colors border',
                  selected && type.value === 'BE' ? 'bg-cyan-50 border-cyan-200 text-cyan-600' : '',
                  selected && type.value === 'biosimilar_pk' ? 'bg-violet-50 border-violet-200 text-violet-600' : '',
                  selected && type.value === 'food_effect' ? 'bg-amber-50 border-amber-200 text-amber-600' : '',
                  !selected ? 'bg-slate-50 border-slate-200 text-slate-400 group-hover:border-slate-300 group-hover:text-slate-500' : '',
                ].join(' ')}>
                  {type.icon}
                </div>

                {/* Content */}
                <div className="flex-1 min-w-0">
                  <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 mb-0.5">
                    <span className={[
                      'font-bold text-base leading-snug',
                      selected ? 'text-slate-900' : 'text-slate-700',
                    ].join(' ')}>
                      {type.label}
                    </span>
                    <span className="text-xs text-slate-400 font-medium">{type.subtitle}</span>
                  </div>

                  <p className="text-[11px] font-semibold text-slate-400 mb-2">{type.reg}</p>
                  <p className="text-xs text-slate-500 leading-relaxed">{type.description}</p>

                  {/* Tags */}
                  <div className="mt-3 flex flex-wrap gap-1.5">
                    {type.badge && (
                      <span className={[
                        'tag',
                        selected
                          ? BADGE_STYLES[type.badge]?.sel
                          : BADGE_STYLES[type.badge]?.unsel,
                      ].join(' ')}>
                        {type.badge}
                      </span>
                    )}
                    <span className={[
                      'tag',
                      selected ? 'bg-slate-100 text-slate-600 border-slate-200' : 'bg-slate-50 text-slate-400 border-slate-100',
                    ].join(' ')}>
                      {type.design}
                    </span>
                  </div>
                </div>
              </div>
            </div>
          </button>
        )
      })}
    </div>
  )
}
