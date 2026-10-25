import { useState } from 'react'
import { CriterionToggle } from '../../components/PopulationBuilder'
import { CandidateBadges, cleanIndication } from '../../components/PatientIndicationPanel'
import { DecisionRow } from '../../components/protocol/DecisionRow'
import { GuidelineSelect } from '../../components/protocol/GuidelineSelect'
import { Activity, Badge, Button, ClipboardList, FileText, Input, Shield, cx } from '../../ui'
import { criterionLines, formatDays, pathwayLabel } from '../../lib/ddiFormat'
import { MAX_INDICATION_LENGTH } from '../types'
import type {
  Card,
  DdiRowData,
  DesignDecisionsCardData,
  GuidelineCardData,
  IndicationCandidateData,
  IndicationCardData,
  InteractionScreeningCardData,
} from '../types'
import { Callout, CardFrame, useCardInert } from './CardFrame'
import { useCardContext } from './CardContext'
import { capitalize, list } from './format'

/* ---------------------------------------------------- interaction_screening */

function StatusDot({ status }: { status: string }) {
  return (
    <span
      aria-hidden
      className={cx('mt-1.5 h-1.5 w-1.5 flex-none rounded-full', status === 'prohibited' ? 'bg-red-500' : status === 'restricted' ? 'bg-amber-500' : 'bg-ink-300')}
    />
  )
}

function DdiRowView({ row, card }: { row: DdiRowData; card: Card }) {
  const ctx = useCardContext()
  const inert = useCardInert(card)
  const pending = ctx.pending?.cardId === card.id && ctx.pending.action === 'toggle_exclusion'
  const lines = criterionLines(row)
  return (
    <li className="space-y-1">
      <CriterionToggle
        pressed={row.selected}
        disabled={inert || pending}
        marker={<StatusDot status={row.status} />}
        onClick={() => void ctx.dispatch(card, 'toggle_exclusion', { text: row.criterion_text, on: !row.selected })}
      >
        {row.criterion_text}
      </CriterionToggle>
      {lines.length > 0 ? (
        <details className="pl-1 text-small text-fg-muted">
          <summary className="cursor-pointer select-none text-fg-subtle hover:text-fg">Why this row</summary>
          <ul className="m-0 mt-1 list-none space-y-1 p-0">
            {lines.map((l, i) => (
              <li key={i} className="break-words">
                {l}
              </li>
            ))}
          </ul>
        </details>
      ) : null}
    </li>
  )
}

export function InteractionScreeningCard({ card }: { card: Card }) {
  const d = card.data as unknown as InteractionScreeningCardData
  const prohibited = list(d.prohibited)
  const restricted = list(d.restricted)
  const permitted = list(d.permitted)
  const pathways = list(d.pathway_keys)
  const none = prohibited.length === 0 && restricted.length === 0

  return (
    <CardFrame card={card} icon={<Shield />} title={d.drug_inn ? `Interaction screening for ${capitalize(d.drug_inn)}` : undefined}>
      {pathways.length > 0 ? (
        <p className="mb-3 flex flex-wrap items-center gap-2 text-small text-fg-muted">
          Pathways read from the label:
          {pathways.map(k => (
            <Badge key={k} variant="info">
              {pathwayLabel(k)}
            </Badge>
          ))}
        </p>
      ) : null}
      {d.summary ? <p className="mb-3 text-small text-fg-muted">{d.summary}</p> : null}

      {/* Only the engine knows whether it fell back: a biologic legitimately has
          no CYP/transporter pathways, and showing the fallback note there would
          contradict the biologic-specific screening above it. */}
      {(d.unresolved_count ?? 0) > 0 ? (
        <div className="mb-3">
          <Callout tone="warning">
            No metabolic or transporter pathway could be identified from the label, so a conservative pan-pathway restriction is applied at the longest window.
            The protocol discloses that this is a fallback rather than a label finding.
          </Callout>
        </div>
      ) : null}

      {none ? (
        <p className="text-small text-fg-muted">No comedication restriction was derived from the label.</p>
      ) : (
        <>
          <p className="mb-2 text-small text-fg-muted">
            Switch a row on to add it to the exclusion criteria.
            <span className="ml-2 inline-flex items-center gap-1">
              <span aria-hidden className="h-1.5 w-1.5 rounded-full bg-red-500" /> prohibited
            </span>
            <span className="ml-2 inline-flex items-center gap-1">
              <span aria-hidden className="h-1.5 w-1.5 rounded-full bg-amber-500" /> restricted
            </span>
          </p>
          {prohibited.length > 0 ? (
            <>
              <p className="mb-1.5 text-eyebrow uppercase text-fg-subtle">Prohibited</p>
              <ul className="m-0 mb-3 list-none space-y-2 p-0">
                {prohibited.map(r => (
                  <DdiRowView key={`${r.key}-${r.criterion_text}`} row={r} card={card} />
                ))}
              </ul>
            </>
          ) : null}
          {restricted.length > 0 ? (
            <>
              <p className="mb-1.5 text-eyebrow uppercase text-fg-subtle">Restricted</p>
              <ul className="m-0 mb-3 list-none space-y-2 p-0">
                {restricted.map(r => (
                  <DdiRowView key={`${r.key}-${r.criterion_text}`} row={r} card={card} />
                ))}
              </ul>
            </>
          ) : null}
        </>
      )}

      {permitted.length > 0 ? (
        <details className="mb-1 text-small text-fg-muted">
          <summary className="cursor-pointer select-none font-medium text-fg-muted hover:text-fg">Permitted comedications ({permitted.length})</summary>
          <ul className="m-0 mt-1 list-disc space-y-0.5 pl-5">
            {permitted.map(r => (
              <li key={`${r.key}-${r.label}`}>{r.label}</li>
            ))}
          </ul>
        </details>
      ) : null}

      <p className="mt-3 text-small text-fg-subtle">
        {typeof d.max_washout_days === 'number' ? `Longest washout: ${formatDays(d.max_washout_days)}. ` : ''}
        {typeof d.screening_history_window_days === 'number' ? `Screening history window: ${formatDays(d.screening_history_window_days)}.` : ''}
      </p>
    </CardFrame>
  )
}

/* ------------------------------------------------------- design_decisions */

export function DesignDecisionsCard({ card }: { card: Card }) {
  const ctx = useCardContext()
  const d = card.data as unknown as DesignDecisionsCardData
  const [changing, setChanging] = useState<string | null>(null)
  const inert = useCardInert(card)
  const saving = ctx.pending?.cardId === card.id && ctx.pending.action === 'set_design_decision'

  const change = async (key: string, value: string) => {
    setChanging(key)
    try {
      await ctx.dispatch(card, 'set_design_decision', { key, value })
    } finally {
      setChanging(null)
    }
  }

  return (
    <CardFrame
      card={card}
      icon={<ClipboardList />}
      title={d.drug_inn ? `Study design for ${capitalize(d.drug_inn)}` : undefined}
      doneLabel="Confirmed"
    >
      <p className="mb-3 text-small text-fg-muted">
        Determined from the product&apos;s safety profile, label and PK. Change any before confirming; each decision is written into the protocol
        with its rationale.
      </p>
      <div className="space-y-2.5">
        {list(d.decisions).map(dec => (
          <DecisionRow
            key={dec.key}
            decision={dec}
            disabled={inert}
            pending={saving && changing === dec.key}
            onChange={value => void change(dec.key, value)}
          />
        ))}
      </div>
    </CardFrame>
  )
}

/* -------------------------------------------------------------- indication */

function CandidateView({ c, card, recommended, selected }: { c: IndicationCandidateData; card: Card; recommended: boolean; selected: boolean }) {
  const ctx = useCardContext()
  const inert = useCardInert(card)
  const pending = ctx.pending?.cardId === card.id && ctx.pending.action === 'set_indication'
  return (
    <li
      className={cx(
        'flex items-start gap-3 rounded-input border px-3.5 py-3',
        selected ? 'border-accent-500 bg-[var(--info-bg)]' : 'border-line',
      )}
    >
      <div className="min-w-0 flex-1">
        <p className="flex flex-wrap items-center gap-2 text-body font-semibold text-fg">
          {c.indication}
          {recommended ? <Badge variant="info">Recommended</Badge> : null}
          {selected ? <Badge variant="success">Chosen</Badge> : null}
        </p>
        <CandidateBadges c={{ on_label: c.on_label, monotherapy: c.monotherapy }} />
        {c.setting_note ? <p className="mt-1.5 text-small text-fg-muted">{c.setting_note}</p> : null}
        {c.rank_reason ? <p className="mt-1 text-small text-fg-muted">{c.rank_reason}</p> : null}
        {typeof c.observed_cv_pct === 'number' || c.pk_variability || c.recruitment ? (
          <p className="mt-1 text-small text-fg-subtle">
            {[
              typeof c.observed_cv_pct === 'number' ? `Observed CV ${c.observed_cv_pct}%` : null,
              c.pk_variability ? `PK variability: ${c.pk_variability}` : null,
              c.recruitment ? `Recruitment: ${c.recruitment}` : null,
            ]
              .filter(Boolean)
              .join(' · ')}
          </p>
        ) : null}
      </div>
      <Button
        size="sm"
        variant="secondary"
        className="flex-none"
        loading={pending}
        disabled={inert || selected}
        aria-label={`Use this indication: ${c.indication}`}
        onClick={() => void ctx.dispatch(card, 'set_indication', { text: c.indication })}
      >
        Use this
      </Button>
    </li>
  )
}

export function IndicationCard({ card }: { card: Card }) {
  const ctx = useCardContext()
  const d = card.data as unknown as IndicationCardData
  const inert = useCardInert(card)
  const [draft, setDraft] = useState('')
  const ranked = list(d.ranked)
  const pending = ctx.pending?.cardId === card.id && ctx.pending.action === 'set_indication'
  const text = cleanIndication(draft).trim()

  if (!d.applicable) {
    return (
      <CardFrame card={card} icon={<Activity />}>
        <Callout tone="neutral">{d.reason ?? 'An indication only applies to a patient biosimilar PK study.'}</Callout>
      </CardFrame>
    )
  }

  return (
    <CardFrame card={card} icon={<Activity />}>
      {d.summary ? <p className="mb-3 text-small text-fg-muted">{d.summary}</p> : null}
      <p className="mb-3 flex flex-wrap items-center gap-2 text-small text-fg-muted">
        {d.drug_class ? <span>Drug class: {d.drug_class}.</span> : null}
        {d.label_verified === true ? <Badge variant="success">Label verified</Badge> : d.label_verified === false ? <Badge variant="warning">Label not verified</Badge> : null}
      </p>

      {list(d.user_indication_warnings).map((w, i) => (
        <div key={i} className="mb-3">
          <Callout tone="warning">{w}</Callout>
        </div>
      ))}

      {d.selected_indication ? (
        <p className="mb-3 text-small text-fg">
          Chosen indication: <span className="font-semibold">{d.selected_indication}</span> (locked into the protocol)
        </p>
      ) : (
        <p className="mb-3 text-small text-fg-muted">No indication chosen yet: the engine picks one when you continue.</p>
      )}

      <ul className="m-0 list-none space-y-2 p-0">
        {ranked.map(c => (
          <CandidateView
            key={c.indication}
            c={c}
            card={card}
            recommended={d.recommended?.indication === c.indication}
            selected={d.selected_indication === c.indication}
          />
        ))}
      </ul>

      <form
        className="mt-3 flex flex-wrap items-center gap-2"
        onSubmit={e => {
          e.preventDefault()
          if (text && !inert) void ctx.dispatch(card, 'set_indication', { text })
        }}
      >
        <div className="min-w-[12rem] flex-1">
          <Input
            aria-label="Your own indication"
            placeholder="Or type your own, e.g. resected stage III melanoma"
            maxLength={MAX_INDICATION_LENGTH}
            value={draft}
            disabled={inert}
            onChange={e => setDraft(cleanIndication(e.target.value))}
          />
        </div>
        <Button type="submit" variant="secondary" loading={pending} disabled={inert || !text}>
          Check this indication
        </Button>
      </form>
    </CardFrame>
  )
}

/* --------------------------------------------------------------- guideline */

export function GuidelineCard({ card }: { card: Card }) {
  const ctx = useCardContext()
  const d = card.data as unknown as GuidelineCardData
  const options = list(d.options)
  const [choice, setChoice] = useState(d.selected)
  const inert = useCardInert(card)
  const pending = ctx.pending?.cardId === card.id && ctx.pending.action === 'set_guideline'
  const label = options.find(o => o.id === choice)?.display_name ?? choice

  return (
    <CardFrame
      card={card}
      icon={<FileText />}
      doneLabel="Chosen"
      footer={
        <Button loading={pending} disabled={inert} onClick={() => void ctx.dispatch(card, 'set_guideline', { guideline_version: choice })}>
          {choice === d.default ? `Keep ${label}` : `Use ${label}`}
        </Button>
      }
    >
      <p className="mb-3 text-small text-fg-muted">The guideline sets the terminology, acceptance framing and citations the protocol is written against.</p>
      <GuidelineSelect
        value={choice}
        onChange={setChoice}
        disabled={inert}
        options={options.map(o => ({
          id: o.id,
          label: `${o.display_name} · ${o.region}`,
          citation: o.citation,
          pathway: o.submission_pathway,
        }))}
      />
      {d.selected && d.selected !== choice ? (
        <p className="mt-2 text-small text-fg-subtle">Currently set: {options.find(o => o.id === d.selected)?.display_name ?? d.selected}.</p>
      ) : null}
    </CardFrame>
  )
}
