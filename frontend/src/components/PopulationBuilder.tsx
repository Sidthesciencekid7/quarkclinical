import { useState } from 'react'
import type { ReactNode } from 'react'
import { Button, Check, Chip, Field, Input, Plus, X, cx } from '../ui'
import type { PopulationConfig, SexType, SubjectType } from '../types'

/** Drug-AGNOSTIC exclusion criteria only. Pathway-specific comedication
 *  criteria come from the server's interaction screening (which reads the
 *  drug's real DailyMed label); the frontend never assumes a pathway. */
export const GENERIC_EXCLUSIONS = [
  'Tobacco or nicotine use within 6 months',
  'Alcohol use > 14 units per week',
  'Prescription medications within 14 days',
  'Blood donation within 3 months',
  'Heavy exercise within 48 hours of dosing',
  'History of drug or alcohol abuse',
]

export interface PopulationLimits {
  age_min: number
  age_max: number
  weight_min: number
  weight_max: number
  cv_min: number
  cv_max: number
}

/** The wizard's input ranges (the chat server enforces the same ones). */
export const DEFAULT_POPULATION_LIMITS: PopulationLimits = {
  age_min: 18,
  age_max: 80,
  weight_min: 40,
  weight_max: 150,
  cv_min: 1,
  cv_max: 120,
}

interface Props {
  value: PopulationConfig
  onChange: (config: PopulationConfig) => void
  /** Validation ranges; default 18-80 y, 40-150 kg, CV 1-120. */
  limits?: PopulationLimits
  /** Drug-agnostic exclusion options offered as toggles (default: the standard six). */
  genericExclusions?: string[]
  disabled?: boolean
  className?: string
}

/**
 * A toggle for a long criterion sentence. Unlike a `Chip` it wraps, so a full
 * exclusion-criterion sentence never overflows a narrow card. `aria-pressed`
 * carries the state.
 */
export function CriterionToggle({
  pressed,
  onClick,
  disabled,
  title,
  marker,
  children,
}: {
  pressed: boolean
  onClick: () => void
  disabled?: boolean
  title?: string
  /** A small leading marker, e.g. a red/amber status dot. */
  marker?: ReactNode
  children: ReactNode
}) {
  return (
    <button
      type="button"
      aria-pressed={pressed}
      disabled={disabled}
      title={title}
      onClick={onClick}
      className={cx(
        'inline-flex max-w-full items-start gap-2 rounded-input border px-3 py-2 text-left text-small leading-snug transition-colors duration-fast',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-offset-2',
        pressed ? 'border-accent-500 bg-[var(--info-bg)] text-fg' : 'border-line bg-surface text-fg-muted hover:bg-surface-hover',
        disabled && 'cursor-not-allowed opacity-60 hover:bg-surface',
      )}
    >
      <span
        aria-hidden
        className={cx(
          'mt-0.5 flex h-4 w-4 flex-none items-center justify-center rounded border',
          pressed ? 'border-accent-600 bg-accent-600 text-white' : 'border-line-strong bg-surface',
        )}
      >
        {pressed ? <Check className="h-3 w-3" /> : null}
      </span>
      {marker}
      <span className="min-w-0 break-words">{children}</span>
    </button>
  )
}

function SectionLabel({ children }: { children: React.ReactNode }) {
  return <p className="mb-2 text-eyebrow uppercase text-fg-subtle">{children}</p>
}

function Divider() {
  return <div className="my-5 border-t border-line" />
}

/** Integer input that clamps to [min, max] and ignores unparsable text. */
function NumberInput({
  label,
  value,
  min,
  max,
  onChange,
  disabled,
}: {
  label: string
  value: number
  min: number
  max: number
  onChange: (n: number) => void
  disabled?: boolean
}) {
  return (
    <Input
      type="number"
      aria-label={label}
      value={value}
      min={min}
      max={max}
      disabled={disabled}
      className="w-24 text-center font-semibold tabular-nums"
      onChange={e => {
        const n = parseInt(e.target.value, 10)
        if (!Number.isNaN(n)) onChange(Math.min(max, Math.max(min, n)))
      }}
    />
  )
}

/**
 * The participants form (age, sex, weight, exclusions, optional design
 * parameters). A pure controlled form with no network access: the chat
 * population card renders it inside its inline editor and sends the changed
 * keys as an `update_population` patch. Drug-specific interaction screening is
 * its own card.
 */
export default function PopulationBuilder({ value, onChange, limits = DEFAULT_POPULATION_LIMITS, genericExclusions = GENERIC_EXCLUSIONS, disabled, className }: Props) {
  const set = <K extends keyof PopulationConfig>(key: K, val: PopulationConfig[K]) => onChange({ ...value, [key]: val })
  const [customDraft, setCustomDraft] = useState('')

  const selected = value.exclusion_presets
  const toggleExclusion = (preset: string) =>
    set('exclusion_presets', selected.includes(preset) ? selected.filter(p => p !== preset) : [...selected, preset])

  // Anything selected that the generic list does not offer: a drug-screening
  // criterion or one the user typed. Kept visible and removable.
  const customSelections = selected.filter(p => !genericExclusions.includes(p))

  const addCustom = () => {
    const text = customDraft.trim()
    if (!text) return
    if (!selected.includes(text)) set('exclusion_presets', [...selected, text])
    setCustomDraft('')
  }

  const cvOverridden = typeof value.cv_intra === 'number' && !Number.isNaN(value.cv_intra)

  return (
    <div className={className}>
      <div>
        <SectionLabel>Subject type</SectionLabel>
        <div className="flex flex-wrap gap-2" role="group" aria-label="Subject type">
          {(['healthy', 'patient'] as SubjectType[]).map(t => (
            <Chip key={t} selected={value.subject_type === t} disabled={disabled} onClick={() => set('subject_type', t)}>
              {t === 'healthy' ? 'Healthy volunteers' : 'Patients'}
            </Chip>
          ))}
        </div>
        <p className="mt-2 text-small text-fg-subtle">
          {value.subject_type === 'healthy'
            ? 'Standard for most BE studies. Exclusion criteria must ensure no clinically relevant disease.'
            : 'Used when healthy-volunteer studies are inappropriate or infeasible (for example narrow therapeutic index drugs).'}
        </p>
      </div>

      <Divider />

      <div>
        <SectionLabel>Demographics</SectionLabel>
        <div className="grid grid-cols-1 gap-5 sm:grid-cols-3">
          <div>
            <p className="mb-2 text-small font-medium text-fg">Age range (years)</p>
            <div className="flex items-center gap-2">
              <NumberInput label="Minimum age" value={value.age_min} min={limits.age_min} max={value.age_max} disabled={disabled} onChange={n => set('age_min', n)} />
              <span aria-hidden className="text-fg-subtle">to</span>
              <NumberInput label="Maximum age" value={value.age_max} min={value.age_min} max={limits.age_max} disabled={disabled} onChange={n => set('age_max', n)} />
            </div>
          </div>

          <div>
            <p className="mb-2 text-small font-medium text-fg">Sex</p>
            <div className="flex flex-wrap gap-2" role="group" aria-label="Sex">
              {(['male', 'female', 'both'] as SexType[]).map(s => (
                <Chip key={s} selected={value.sex === s} disabled={disabled} onClick={() => set('sex', s)}>
                  {s === 'both' ? 'Both' : s === 'male' ? 'Male' : 'Female'}
                </Chip>
              ))}
            </div>
          </div>

          <div>
            <p className="mb-2 text-small font-medium text-fg">Weight range (kg)</p>
            <div className="flex items-center gap-2">
              <NumberInput label="Minimum weight" value={value.weight_min} min={limits.weight_min} max={value.weight_max} disabled={disabled} onChange={n => set('weight_min', n)} />
              <span aria-hidden className="text-fg-subtle">to</span>
              <NumberInput label="Maximum weight" value={value.weight_max} min={value.weight_min} max={limits.weight_max} disabled={disabled} onChange={n => set('weight_max', n)} />
            </div>
          </div>
        </div>
      </div>

      <Divider />

      <div>
        <div className="mb-2 flex items-center justify-between">
          <SectionLabel>Exclusion criteria</SectionLabel>
          <span className="text-small text-fg-subtle">{selected.length} selected</span>
        </div>
        <p className="mb-3 text-small text-fg-subtle">Selected criteria are included verbatim in the Subject Selection section.</p>

        <div className="flex flex-wrap gap-2" role="group" aria-label="General eligibility criteria">
          {genericExclusions.map(preset => (
            <CriterionToggle key={preset} pressed={selected.includes(preset)} disabled={disabled} onClick={() => toggleExclusion(preset)}>
              {preset}
            </CriterionToggle>
          ))}
        </div>

        {customSelections.length > 0 ? (
          <>
            <p className="mb-2 mt-4 text-eyebrow uppercase text-fg-subtle">Other criteria</p>
            <ul className="m-0 flex list-none flex-wrap gap-2 p-0">
              {customSelections.map(text => (
                <li key={text} className="inline-flex max-w-full items-start gap-2 rounded-input border border-accent-500 bg-[var(--info-bg)] px-3 py-2 text-small leading-snug text-fg">
                  <span className="min-w-0 break-words">{text}</span>
                  <button
                    type="button"
                    disabled={disabled}
                    aria-label={`Remove criterion: ${text}`}
                    title="Remove"
                    onClick={() => set('exclusion_presets', selected.filter(p => p !== text))}
                    className="-mr-1 flex h-5 w-5 flex-none items-center justify-center rounded-full text-fg-muted hover:bg-line hover:text-fg"
                  >
                    <X aria-hidden className="h-3 w-3" />
                  </button>
                </li>
              ))}
            </ul>
          </>
        ) : null}

        <div className="mt-3 flex gap-2">
          <Input
            aria-label="Add your own exclusion criterion"
            placeholder="Add your own exclusion criterion"
            value={customDraft}
            disabled={disabled}
            maxLength={300}
            onChange={e => setCustomDraft(e.target.value)}
            onKeyDown={e => {
              if (e.key === 'Enter') {
                e.preventDefault()
                addCustom()
              }
            }}
          />
          <Button variant="secondary" leftIcon={<Plus />} disabled={disabled || !customDraft.trim()} onClick={addCustom}>
            Add
          </Button>
        </div>
      </div>

      <Divider />

      <div>
        <SectionLabel>Study design parameters (optional)</SectionLabel>
        <p className="mb-3 text-small text-fg-subtle">
          Supplying these refines the engine-computed sample size and design. Left blank, the within-subject CV is taken from the
          literature and the reference product and strength are written as placeholders.
        </p>

        <div
          className={cx(
            'mb-4 rounded-input border px-3 py-2.5 text-small',
            cvOverridden
              ? 'border-[color:var(--warn-border)] bg-[var(--warn-bg)] text-[color:var(--warn-fg)]'
              : 'border-line bg-surface-muted text-fg-muted',
          )}
        >
          {cvOverridden ? (
            <p>
              <span className="font-semibold">An investigator-specified CV overrides the literature value.</span> The study will be powered on{' '}
              {value.cv_intra}% instead of the highest within-subject CV reported in the healthy-volunteer literature, and the protocol will
              declare it as investigator-specified. Clear the field to return to the sourced value.
            </p>
          ) : (
            <p>
              <span className="font-medium text-fg">Within-subject CV is retrieved automatically.</span> Left blank (recommended), the sample
              size is powered on the highest within-subject CV reported for Cmax or AUC in the healthy-volunteer literature. Enter a value only
              to override that sourced figure.
            </p>
          )}
        </div>

        <div className="grid grid-cols-1 gap-x-5 gap-y-4 sm:grid-cols-2">
          <Field label="Within-subject CV (%)" hint="Blank = use the literature value.">
            <Input
              type="number"
              min={limits.cv_min}
              max={limits.cv_max}
              step={0.5}
              disabled={disabled}
              value={value.cv_intra ?? ''}
              placeholder="Blank = literature value"
              onChange={e => {
                const n = parseFloat(e.target.value)
                set('cv_intra', e.target.value === '' || Number.isNaN(n) ? undefined : n)
              }}
            />
          </Field>

          <Field label="Dose strength">
            <Input
              disabled={disabled}
              value={value.dose_strength ?? ''}
              placeholder="e.g. 400 mg film-coated tablet"
              maxLength={200}
              onChange={e => set('dose_strength', e.target.value || undefined)}
            />
          </Field>

          <Field label="Reference listed drug (RLD)" className="sm:col-span-2">
            <Input
              disabled={disabled}
              value={value.reference_product ?? ''}
              placeholder="e.g. Brand 400 mg (Manufacturer)"
              maxLength={200}
              onChange={e => set('reference_product', e.target.value || undefined)}
            />
          </Field>
        </div>

        <label className="mt-4 flex cursor-pointer select-none items-start gap-2.5">
          <input
            type="checkbox"
            checked={value.is_nti === true}
            disabled={disabled}
            onChange={e => set('is_nti', e.target.checked ? true : undefined)}
            className="mt-1 h-4 w-4 cursor-pointer rounded border-line accent-[var(--accent-600,#0891b2)]"
          />
          <span className="text-small text-fg-muted">
            <span className="font-semibold text-fg">Narrow therapeutic index (NTI) drug.</span> Applies tightened 90.00-111.11% acceptance
            limits and a fully replicated design with reference scaling (for example warfarin, tacrolimus, levothyroxine).
          </span>
        </label>
      </div>

      <Divider />

      <div>
        <SectionLabel>Additional sampling timepoints (optional)</SectionLabel>
        <p className="mb-3 text-small text-fg-subtle">
          Extra draws added on top of the engine-computed schedule; the computed timepoints are kept unchanged.
        </p>
        <div className="grid grid-cols-1 gap-x-5 gap-y-4 sm:grid-cols-2">
          <Field label="Additional PK timepoints (hours post-dose)" hint="Comma-separated hours, e.g. 36, 48, 72.">
            <Input
              disabled={disabled}
              value={value.additional_pk_timepoints_hours ?? ''}
              placeholder="e.g. 36, 48, 72"
              maxLength={200}
              onChange={e => set('additional_pk_timepoints_hours', e.target.value || undefined)}
            />
          </Field>
          <Field
            label="Additional immunogenicity (ADA) timepoints (months)"
            hint="Comma-separated months. Only used when a comparative immunogenicity schedule is generated (multiple-dose biosimilar, patients)."
          >
            <Input
              disabled={disabled}
              value={value.additional_immunogenicity_timepoints_months ?? ''}
              placeholder="e.g. 9, 15"
              maxLength={200}
              onChange={e => set('additional_immunogenicity_timepoints_months', e.target.value || undefined)}
            />
          </Field>
        </div>
      </div>
    </div>
  )
}
