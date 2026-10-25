import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { ProtocolPickerList } from '../../components/BudgetProtocolPicker'
import { ReviewTable } from '../../components/protocol/ReviewTable'
import { Badge, Button, ButtonLink, Calculator, Field, Input, LinkIcon, Pencil, Receipt, Select, X } from '../../ui'
import { studyTypeLabel } from '../../lib/studyTypes'
import type { BudgetPreviewCardData, BudgetReadyCardData, BudgetSummaryCardData, Card, ProtocolOption } from '../types'
import { BulletList, Callout, CardFrame, SummaryTable, useCardInert } from './CardFrame'
import { useCardContext } from './CardContext'
import { capitalize, formatMonths, formatMoney, list, previewHint, shortDate } from './format'

function MoneyFigure({ label, value, currency }: { label: string; value: number | null | undefined; currency: string }) {
  return (
    <div className="rounded-input bg-surface-muted px-4 py-3">
      <p className="text-eyebrow uppercase text-fg-subtle">{label}</p>
      <p className="mt-0.5 text-h2 tabular-nums text-fg">{formatMoney(value, currency)}</p>
    </div>
  )
}

/* ---------------------------------------------------------- budget_preview */

const PROFILE_LABEL: Record<string, string> = { healthy: 'Healthy volunteers', patient: 'Patients' }

export function BudgetPreviewCard({ card }: { card: Card }) {
  const d = card.data as unknown as BudgetPreviewCardData
  const x = d.defaults ?? ({} as BudgetPreviewCardData['defaults'])
  const num = (label: string, v: number | null | undefined, unit = '') => (v == null ? [] : [{ label, value: `${typeof v === 'number' && !Number.isInteger(v) ? formatMonths(v) : v}${unit}` }])
  const rows = [
    { label: 'Study type', value: d.study_type ? studyTypeLabel(d.study_type) : 'Engine default' },
    { label: 'Participants', value: PROFILE_LABEL[d.subject_profile] ?? d.subject_profile },
    ...(d.therapeutic_area && d.therapeutic_area !== 'unknown' ? [{ label: 'Therapeutic area', value: capitalize(d.therapeutic_area) }] : []),
    ...num('Enrolled', x.n_enrolled),
    ...num('Sites', x.n_sites),
    ...num('Countries', x.n_countries),
    ...num('Project duration', x.project_duration_months, ' months'),
    ...num('Start-up', x.startup_months, ' months'),
    ...num('Recruitment', x.recruitment_months, ' months'),
    ...num('Treatment', x.treatment_months, ' months'),
    ...num('Follow-up', x.followup_months, ' months'),
    ...num('Close-out', x.closeout_months, ' months'),
    ...num('Expected SAEs', x.n_expected_saes),
    ...(x.geography_profile ? [{ label: 'Geography', value: x.geography_profile.replace(/_/g, ' ') }] : []),
    { label: 'Cost categories', value: String(d.category_count ?? 0) },
  ]
  return (
    <CardFrame card={card} icon={<Calculator />} title={d.drug_inn ? `Budget estimate for ${capitalize(d.drug_inn)}` : undefined}>
      <p className="mb-3 text-small text-fg-muted">{previewHint(d.drug_inn ?? '', d)}</p>
      <MoneyFigure label="Estimated total (planning estimate)" value={d.estimated_grand_total} currency={d.currency || 'USD'} />
      <div className="mt-3">
        <ReviewTable ariaLabel="Budget profile" rows={rows} />
      </div>
      {d.disclaimer ? <p className="mt-3 text-small text-fg-subtle">{d.disclaimer}</p> : null}
    </CardFrame>
  )
}

/* ---------------------------------------------------------- budget_summary */

const CURRENCIES = ['USD', 'EUR', 'GBP', 'CAD', 'AUD', 'CHF', 'JPY']
const EDIT_FIELDS: Array<{ path: string; label: string; numeric?: boolean; hint?: string }> = [
  { path: 'name', label: 'Budget name' },
  { path: 'sponsor_name', label: 'Sponsor' },
  { path: 'cro_name', label: 'CRO' },
  { path: 'n_sites_selected', label: 'Sites', numeric: true, hint: 'Patient studies only.' },
  { path: 'n_countries', label: 'Countries', numeric: true, hint: 'Patient studies only.' },
  { path: 'n_enrolled', label: 'Patients enrolled', numeric: true, hint: 'Patient studies only.' },
]

function optionLabel(p: ProtocolOption): string {
  return `${capitalize(p.drug_inn)} (${studyTypeLabel(p.study_type)}, v${p.version})`
}

export function BudgetSummaryCard({ card }: { card: Card }) {
  const ctx = useCardContext()
  const d = card.data as unknown as BudgetSummaryCardData
  const inert = useCardInert(card)
  const rows = list(d.rows)
  const options = list(d.protocol_options)
  const linked = d.linked_protocol
  const [editing, setEditing] = useState(false)
  const pendingLink = ctx.pending?.cardId === card.id && ctx.pending.action === 'link_protocol'
  const pendingSave = ctx.pending?.cardId === card.id && ctx.pending.action === 'set_budget_fields'

  const rowValue = (path: string): string => rows.find(r => r.path === path)?.value ?? ''
  const initial = (): Record<string, string> => ({
    currency: d.currency || rowValue('currency') || 'USD',
    ...Object.fromEntries(EDIT_FIELDS.map(f => [f.path, rowValue(f.path)])),
  })
  const [draft, setDraft] = useState<Record<string, string>>(initial)
  useEffect(() => {
    if (!editing) setDraft(initial())
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [d, editing])

  const patch = (): Record<string, unknown> => {
    const base = initial()
    const out: Record<string, unknown> = {}
    for (const [k, v] of Object.entries(draft)) {
      if (v === base[k]) continue
      const field = EDIT_FIELDS.find(f => f.path === k)
      if (field?.numeric) out[k] = v.trim() === '' ? null : Number(v)
      else out[k] = v.trim() === '' && k !== 'currency' ? null : v.trim()
    }
    return out
  }
  const changes = patch()
  const dirty = Object.keys(changes).length > 0

  const save = async () => {
    const res = await ctx.dispatch(card, 'set_budget_fields', { patch: changes })
    if (res) setEditing(false)
  }

  return (
    <CardFrame
      card={card}
      icon={<Receipt />}
      doneLabel="Created"
      footer={
        editing ? (
          <>
            <Button variant="ghost" disabled={inert} onClick={() => setEditing(false)}>
              Cancel
            </Button>
            <Button variant="secondary" loading={pendingSave} disabled={inert || !dirty} onClick={() => void save()}>
              Save details
            </Button>
          </>
        ) : (
          <Button variant="secondary" leftIcon={<Pencil />} disabled={inert} onClick={() => setEditing(true)}>
            Edit details
          </Button>
        )
      }
    >
      {typeof d.estimated_grand_total === 'number' ? (
        <div className="mb-3">
          <MoneyFigure label="Estimated total (planning estimate)" value={d.estimated_grand_total} currency={d.currency || 'USD'} />
        </div>
      ) : null}

      {editing ? (
        <div className="grid grid-cols-1 gap-x-4 gap-y-3 sm:grid-cols-2">
          <Field label="Currency">
            <Select value={draft.currency} onChange={e => setDraft(s => ({ ...s, currency: e.target.value }))} disabled={inert}>
              {CURRENCIES.map(c => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </Select>
          </Field>
          {EDIT_FIELDS.map(f => (
            <Field key={f.path} label={f.label} hint={f.hint}>
              <Input
                type={f.numeric ? 'number' : 'text'}
                min={f.numeric ? 0 : undefined}
                maxLength={f.numeric ? undefined : 120}
                value={draft[f.path] ?? ''}
                disabled={inert}
                onChange={e => setDraft(s => ({ ...s, [f.path]: e.target.value }))}
              />
            </Field>
          ))}
        </div>
      ) : (
        <SummaryTable rows={rows} ariaLabel="Budget summary" />
      )}

      <div className="mt-4">
        <p className="mb-1.5 text-eyebrow uppercase text-fg-subtle">Linked protocol</p>
        {linked ? (
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant="info" icon={<LinkIcon />}>
              {optionLabel(linked)}
            </Badge>
            <Button
              variant="ghost"
              size="sm"
              leftIcon={<X />}
              loading={pendingLink}
              disabled={inert}
              onClick={() => void ctx.dispatch(card, 'link_protocol', { protocol_id: null })}
            >
              Unlink
            </Button>
          </div>
        ) : options.length === 0 ? (
          <div className="flex flex-wrap items-center gap-3">
            <p className="text-small text-fg-muted">None. A budget works without one; linking a protocol sharpens the estimate.</p>
            <Button variant="secondary" size="sm" leftIcon={<LinkIcon />} disabled={inert} onClick={() => ctx.sendText('Link a protocol')}>
              Link a protocol
            </Button>
          </div>
        ) : null}
        {options.length > 0 ? (
          <ProtocolPickerList
            className="mt-2"
            value={linked?.id ?? null}
            disabled={inert || pendingLink}
            items={options.map(p => ({
              id: p.id,
              drugInn: p.drug_inn,
              studyType: p.study_type,
              meta: `v${p.version} · ${p.status}${p.created_at ? ` · ${shortDate(p.created_at)}` : ''}`,
            }))}
            onChange={id => void ctx.dispatch(card, 'link_protocol', { protocol_id: id })}
          />
        ) : null}
      </div>

      {!d.ready && list(d.missing).length > 0 ? (
        <div className="mt-3">
          <Callout tone="warning">
            <p className="font-semibold">Still needed:</p>
            <BulletList items={d.missing} className="mt-1 text-[color:inherit]" />
          </Callout>
        </div>
      ) : null}
      {d.blocking_issue ? (
        <div className="mt-3">
          <Callout tone="danger">{d.blocking_issue}</Callout>
        </div>
      ) : null}
      <BulletList items={d.notes} className="mt-3" />
    </CardFrame>
  )
}

/* ------------------------------------------------------------ budget_ready */

export function BudgetReadyCard({ card }: { card: Card }) {
  const d = card.data as unknown as BudgetReadyCardData
  const rows = [
    { label: 'Name', value: d.name },
    ...(d.drug_inn ? [{ label: 'Drug', value: capitalize(d.drug_inn) }] : []),
    ...(d.study_type ? [{ label: 'Study type', value: studyTypeLabel(d.study_type) }] : []),
    ...(d.n_enrolled != null ? [{ label: 'Enrolled', value: String(d.n_enrolled) }] : []),
    { label: 'Cost categories', value: String(d.category_count ?? 0) },
  ]
  return (
    <CardFrame
      card={card}
      icon={<Receipt />}
      doneLabel="Ready"
      hideServerActions
      footer={
        <ButtonLink as={Link} to={`/app/budgets/${encodeURIComponent(d.budget_id)}`} state={{ justCreated: true }} variant="primary" leftIcon={<Calculator />}>
          Open budget
        </ButtonLink>
      }
    >
      <MoneyFigure label="Budget total" value={d.grand_total} currency={d.currency || 'USD'} />
      <div className="mt-3">
        <ReviewTable ariaLabel="Budget" rows={rows} />
      </div>
      <p className="mt-3 text-small text-fg-subtle">Every line is editable in the budget editor; the total there is the same figure.</p>
    </CardFrame>
  )
}
