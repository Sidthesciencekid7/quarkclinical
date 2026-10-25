import { useEffect, useId, useState } from 'react'
import DrugSelector from '../../components/DrugSelector'
import PopulationBuilder, { CriterionToggle, GENERIC_EXCLUSIONS } from '../../components/PopulationBuilder'
import { Badge, Button, Check, FlaskConical, Pencil, Pill, Search, Users, X, cx } from '../../ui'
import { ReviewTable } from '../../components/protocol/ReviewTable'
import { STUDY_TYPE_DETAILS, studyTypeLabel } from '../../lib/studyTypes'
import type { PopulationConfig } from '../../types'
import type {
  Card,
  CvOverrideWarningCardData,
  DrugAddOfferCardData,
  DrugMatchesCardData,
  Modality,
  PopulationCardData,
  PopulationValues,
  StudyTypeCardData,
} from '../types'
import { Callout, CardFrame, BulletList, useCardInert } from './CardFrame'
import { useCardContext } from './CardContext'
import { capitalize, list } from './format'

const MODALITY_LABEL: Record<Modality, string> = {
  biologic: 'Biologic',
  small_molecule: 'Small molecule',
  unknown: 'Unclassified',
}

function ModalityBadge({ modality }: { modality: Modality | undefined }) {
  if (!modality || modality === 'unknown') return null
  return <Badge variant="neutral">{MODALITY_LABEL[modality] ?? modality}</Badge>
}

function studyTypeList(types: string[] | undefined): string {
  return list(types).map(studyTypeLabel).join(', ')
}

function usePendingFor(card: Card, action: string): boolean {
  const ctx = useCardContext()
  return ctx.pending?.cardId === card.id && ctx.pending.action === action
}

/* --------------------------------------------------------- drug_matches */

export function DrugMatchesCard({ card }: { card: Card }) {
  const ctx = useCardContext()
  const d = card.data as unknown as DrugMatchesCardData
  const matches = list(d.matches)
  const [searching, setSearching] = useState(false)
  const inert = useCardInert(card)
  const loading = usePendingFor(card, 'select_drug')

  return (
    <CardFrame
      card={card}
      icon={<Pill />}
      footer={
        !searching ? (
          <Button variant="ghost" size="sm" leftIcon={<Search />} disabled={inert} onClick={() => setSearching(true)}>
            Search for another drug
          </Button>
        ) : null
      }
    >
      {d.query ? <p className="mb-2 text-small text-fg-muted">Catalogue results for &ldquo;{d.query}&rdquo;</p> : null}

      {matches.length === 0 ? <p className="text-small text-fg-muted">No drug in the catalogue matched.</p> : null}
      <ul className="m-0 list-none divide-y divide-line overflow-hidden rounded-input border border-line p-0">
        {matches.map(m => {
          const selected = d.selected_drug_id === m.id
          return (
            <li key={m.id} className="flex flex-wrap items-center gap-3 px-3.5 py-3">
              <div className="min-w-0 flex-1">
                <p className="flex flex-wrap items-center gap-2 text-body font-semibold capitalize text-fg">
                  {m.inn}
                  <ModalityBadge modality={m.modality} />
                  {selected ? <Badge variant="success" icon={<Check />}>Selected</Badge> : null}
                </p>
                {list(m.brand_names).length > 0 ? <p className="text-small text-fg-muted">{list(m.brand_names).join(' · ')}</p> : null}
                {list(m.allowed_study_types).length > 0 ? (
                  <p className="text-small text-fg-subtle">Study types: {studyTypeList(m.allowed_study_types)}</p>
                ) : null}
              </div>
              <Button
                size="sm"
                variant={selected ? 'secondary' : 'primary'}
                loading={loading}
                disabled={selected || inert}
                aria-label={`Use ${m.inn}`}
                onClick={() => void ctx.dispatch(card, 'select_drug', { drug_id: m.id })}
              >
                {selected ? 'Selected' : `Use ${m.inn}`}
              </Button>
            </li>
          )
        })}
      </ul>
      {d.truncated ? <p className="mt-2 text-small text-fg-subtle">More drugs matched than are listed. Search to narrow them down.</p> : null}

      {searching ? (
        <div className="mt-3">
          <div className="mb-2 flex items-center justify-between">
            <p className="text-small font-medium text-fg">Search the catalogue</p>
            <Button variant="ghost" size="sm" leftIcon={<X />} onClick={() => setSearching(false)}>
              Close
            </Button>
          </div>
          <DrugSelector
            autoFocus
            disabled={inert}
            onChange={drug => {
              if (drug) void ctx.dispatch(card, 'select_drug', { drug_id: drug.id })
            }}
            onAddRequest={inn => ctx.sendText(`Add ${inn} to the catalogue`)}
          />
        </div>
      ) : null}
    </CardFrame>
  )
}

/* ------------------------------------------------------- drug_add_offer */

export function DrugAddOfferCard({ card }: { card: Card }) {
  const d = card.data as unknown as DrugAddOfferCardData
  return (
    <CardFrame card={card} icon={<Pill />}>
      <Callout tone={d.valid ? 'info' : 'warning'}>{d.message}</Callout>
      {d.valid && (d.modality !== 'unknown' || list(d.allowed_study_types).length > 0) ? (
        <p className="mt-3 flex flex-wrap items-center gap-2 text-small text-fg-muted">
          <ModalityBadge modality={d.modality} />
          {list(d.allowed_study_types).length > 0 ? <span>Study types it can run: {studyTypeList(d.allowed_study_types)}</span> : null}
        </p>
      ) : null}
    </CardFrame>
  )
}

/* ------------------------------------------------------------ study_type */

export function StudyTypeCard({ card }: { card: Card }) {
  const ctx = useCardContext()
  const d = card.data as unknown as StudyTypeCardData
  const options = list(d.options)
  const loading = usePendingFor(card, 'set_study_type')
  const reasonPrefix = useId()

  return (
    <CardFrame card={card} icon={<FlaskConical />} title={d.drug_inn ? `Study type for ${capitalize(d.drug_inn)}` : undefined}>
      {d.gate_message ? (
        <div className="mb-3">
          <Callout tone="warning">{d.gate_message}</Callout>
        </div>
      ) : null}
      <div role="group" aria-label="Study type" className="space-y-2.5">
        {options.map(o => {
          const detail = STUDY_TYPE_DETAILS[o.value]
          const selected = d.selected === o.value
          const reasonId = `${reasonPrefix}-${o.value}`
          return (
            <button
              key={o.value}
              type="button"
              disabled={!o.allowed}
              aria-pressed={selected}
              aria-describedby={!o.allowed && o.reason ? reasonId : undefined}
              data-loading={loading || undefined}
              onClick={() => void ctx.dispatch(card, 'set_study_type', { study_type: o.value })}
              className={cx(
                'block w-full rounded-input border px-4 py-3 text-left transition-colors duration-fast',
                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-offset-2',
                selected ? 'border-accent-500 bg-[var(--info-bg)]' : 'border-line bg-surface hover:bg-surface-hover',
                !o.allowed && 'cursor-not-allowed opacity-60 hover:bg-surface',
              )}
            >
              <span className="flex flex-wrap items-center gap-2">
                <span className="text-body font-semibold text-fg">{o.label || detail?.label || studyTypeLabel(o.value)}</span>
                {detail ? <span className="text-small text-fg-subtle">{detail.subtitle}</span> : null}
                {selected ? <Badge variant="success" icon={<Check />}>Selected</Badge> : null}
                {!o.allowed ? <Badge variant="neutral">Not available</Badge> : null}
              </span>
              {detail ? <span className="mt-1 block text-small text-fg-muted">{detail.description}</span> : null}
              {detail ? <span className="mt-1 block text-small text-fg-subtle">{detail.design}</span> : null}
              {!o.allowed && o.reason ? (
                <span id={reasonId} className="mt-1.5 block text-small font-medium text-[color:var(--warn-fg)]">
                  {o.reason}
                </span>
              ) : null}
            </button>
          )
        })}
      </div>
    </CardFrame>
  )
}

/* ------------------------------------------------------------ population */

const FIELD_PATH = (key: string) => `population.${key}`

/** Server values (nulls) -> the form's PopulationConfig (undefined for blank). */
export function configFromValues(v: PopulationValues): PopulationConfig {
  return {
    subject_type: v.subject_type,
    age_min: v.age_min,
    age_max: v.age_max,
    sex: v.sex,
    weight_min: v.weight_min,
    weight_max: v.weight_max,
    exclusion_presets: list(v.exclusion_presets),
    cv_intra: v.cv_intra ?? undefined,
    dose_strength: v.dose_strength ?? undefined,
    reference_product: v.reference_product ?? undefined,
    is_nti: v.is_nti ?? undefined,
    additional_pk_timepoints_hours: v.additional_pk_timepoints_hours ?? undefined,
    additional_immunogenicity_timepoints_months: v.additional_immunogenicity_timepoints_months ?? undefined,
  }
}

const OPTIONAL_KEYS = ['cv_intra', 'dose_strength', 'reference_product', 'is_nti', 'additional_pk_timepoints_hours', 'additional_immunogenicity_timepoints_months'] as const
const REQUIRED_KEYS = ['subject_type', 'age_min', 'age_max', 'sex', 'weight_min', 'weight_max'] as const

/** The `update_population` patch for an edit: only the keys that changed; a cleared optional field is `null`. */
export function populationPatch(values: PopulationValues, draft: PopulationConfig): Record<string, unknown> {
  const patch: Record<string, unknown> = {}
  for (const k of REQUIRED_KEYS) if (draft[k] !== values[k]) patch[k] = draft[k]
  for (const k of OPTIONAL_KEYS) {
    const before = values[k] ?? null
    const after = (draft[k] as unknown) ?? null
    if (before !== after) patch[k] = after
  }
  const a = list(values.exclusion_presets)
  const b = list(draft.exclusion_presets)
  if (a.length !== b.length || a.some((x, i) => x !== b[i])) patch.exclusion_presets = b
  return patch
}

const SEX_LABEL = { male: 'Male only', female: 'Female only', both: 'Both sexes' } as const

export function PopulationCard({ card }: { card: Card }) {
  const ctx = useCardContext()
  const d = card.data as unknown as PopulationCardData
  const v = d.values
  const inert = useCardInert(card)
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState<PopulationConfig>(() => configFromValues(v))
  const savingEdit = usePendingFor(card, 'update_population')
  const toggling = ctx.pending?.cardId === card.id && ctx.pending.action === 'toggle_exclusion'

  // Keep the form in step with the server's values while the editor is closed.
  useEffect(() => {
    if (!editing) setDraft(configFromValues(v))
  }, [v, editing])

  const status = (key: string): string | undefined => d.field_status?.[FIELD_PATH(key)]
  const stateBadge = (key: string) => {
    const s = status(key)
    if (!s || s === 'confirmed') return undefined
    return <Badge variant={s === 'proposed' ? 'warning' : 'neutral'}>{s === 'proposed' ? 'Check' : s === 'engine' ? 'Engine' : 'Default'}</Badge>
  }

  const patch = populationPatch(v, draft)
  const dirty = Object.keys(patch).length > 0

  const generic = list(d.generic_exclusion_options).length > 0 ? list(d.generic_exclusion_options) : GENERIC_EXCLUSIONS
  const selected = list(v.exclusion_presets)
  const extras = selected.filter(t => !generic.includes(t))
  const exclusionChips = [...generic, ...extras]

  const rows = [
    { label: 'Participants', value: v.subject_type === 'patient' ? 'Patients' : 'Healthy volunteers', badge: stateBadge('subject_type') },
    { label: 'Age', value: `${v.age_min} to ${v.age_max} years`, badge: stateBadge('age_min') },
    { label: 'Sex', value: SEX_LABEL[v.sex] ?? v.sex, badge: stateBadge('sex') },
    { label: 'Weight', value: `${v.weight_min} to ${v.weight_max} kg`, badge: stateBadge('weight_min') },
    ...(v.cv_intra != null
      ? [{ label: 'Within-subject CV', value: `${v.cv_intra}%`, badge: <Badge variant="warning">Your override</Badge>, highlight: true }]
      : []),
    ...(v.dose_strength ? [{ label: 'Dose strength', value: v.dose_strength, badge: stateBadge('dose_strength') }] : []),
    ...(v.reference_product ? [{ label: 'Reference product', value: v.reference_product, badge: stateBadge('reference_product') }] : []),
    ...(v.is_nti ? [{ label: 'Narrow therapeutic index', value: 'Yes (tightened limits)', badge: stateBadge('is_nti') }] : []),
    ...(v.additional_pk_timepoints_hours ? [{ label: 'Extra PK timepoints', value: `${v.additional_pk_timepoints_hours} h`, badge: undefined }] : []),
    ...(v.additional_immunogenicity_timepoints_months ? [{ label: 'Extra ADA timepoints', value: `${v.additional_immunogenicity_timepoints_months} months`, badge: undefined }] : []),
  ]

  const save = async () => {
    const res = await ctx.dispatch(card, 'update_population', { patch })
    if (res) setEditing(false)
  }

  return (
    <CardFrame
      card={card}
      icon={<Users />}
      title={d.drug_inn ? `Participants for ${capitalize(d.drug_inn)}` : undefined}
      footer={
        editing ? (
          <>
            <Button variant="ghost" disabled={inert} onClick={() => { setDraft(configFromValues(v)); setEditing(false) }}>
              Cancel
            </Button>
            <Button variant="primary" loading={savingEdit} disabled={inert || !dirty} onClick={() => void save()}>
              Save changes
            </Button>
          </>
        ) : (
          <>
            <Button variant="secondary" leftIcon={<Pencil />} disabled={inert} onClick={() => setEditing(true)}>
              Edit
            </Button>
            <Button
              variant="primary"
              loading={savingEdit}
              disabled={inert}
              onClick={() => void ctx.dispatch(card, 'update_population', { patch: d.confirm_patch })}
            >
              Looks good
            </Button>
          </>
        )
      }
      hideServerActions
    >
      {editing ? (
        <PopulationBuilder
          value={draft}
          onChange={setDraft}
          limits={d.ranges}
          genericExclusions={generic}
          disabled={inert}
        />
      ) : (
        <>
          <ReviewTable ariaLabel="Participants" rows={rows} />
          <p className="mb-2 mt-4 text-eyebrow uppercase text-fg-subtle">Exclusion criteria ({selected.length} selected)</p>
          <div role="group" aria-label="Exclusion criteria" className="flex flex-wrap gap-2">
            {exclusionChips.map(text => {
              const on = selected.includes(text)
              return (
                <CriterionToggle
                  key={text}
                  pressed={on}
                  disabled={inert || toggling}
                  onClick={() => void ctx.dispatch(card, 'toggle_exclusion', { text, on: !on })}
                >
                  {text}
                </CriterionToggle>
              )
            })}
          </div>
          <p className="mt-2 text-small text-fg-subtle">
            Drug-specific interaction criteria appear in their own card once you confirm the participants.
          </p>
        </>
      )}
    </CardFrame>
  )
}

/* ---------------------------------------------------- cv_override_warning */

export function CvOverrideWarningCard({ card }: { card: Card }) {
  const d = card.data as unknown as CvOverrideWarningCardData
  return (
    <CardFrame card={card} icon={<FlaskConical />}>
      <Callout tone="warning">
        <p className="font-semibold">Within-subject CV set to {d.cv_intra}%</p>
        <p className="mt-1">{d.message}</p>
      </Callout>
      <BulletList items={d.consequences} className="mt-3" />
    </CardFrame>
  )
}
