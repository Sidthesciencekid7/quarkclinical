import type { ReactNode } from 'react'
import { Badge, Button, ChatCard } from '../../ui'
import type { BadgeVariant, ButtonVariant } from '../../ui'
import { ReviewTable } from '../../components/protocol/ReviewTable'
import type { ActionType, Card, CardAction, FieldStatus, SummaryRow } from '../types'
import { useCardContext } from './CardContext'
import { isActionType, payloadForCardAction } from './payloads'
import { list } from './format'

const BUTTON_VARIANT: Record<string, ButtonVariant> = {
  primary: 'primary',
  secondary: 'secondary',
  ghost: 'ghost',
  danger: 'danger',
}

/** True when the card's own controls must be inert: it is no longer the live
 *  card (done / superseded / error) or a reply is in flight. */
export function useCardInert(card: Card): boolean {
  const ctx = useCardContext()
  return card.status !== 'active' || ctx.busy
}

export interface CardFrameProps {
  card: Card
  icon: ReactNode
  /** Overrides `card.title`. */
  title?: ReactNode
  /** Badge text for a finished card (default "Done"). */
  doneLabel?: string
  children?: ReactNode
  /** Extra footer buttons, shown before the server-declared actions. */
  footer?: ReactNode
  /** Do not render the server-declared `card.actions` as buttons. */
  hideServerActions?: boolean
  /** Extra classes (e.g. an accent border). */
  className?: string
}

/**
 * Shared frame of every card: header with the status badge, the body inside a
 * disabled fieldset when the card is not live, the server-declared buttons
 * (mapped to the payloads in `payloads.ts`) and the last error of an action.
 */
export function CardFrame({ card, icon, title, doneLabel = 'Done', children, footer, hideServerActions, className }: CardFrameProps) {
  const ctx = useCardContext()
  const inert = card.status !== 'active' || ctx.busy
  const superseded = card.status === 'superseded'
  const error = ctx.errors[card.id]

  const badge =
    card.status === 'done' ? (
      <Badge variant="success">{doneLabel}</Badge>
    ) : card.status === 'superseded' ? (
      <Badge variant="neutral">Replaced</Badge>
    ) : card.status === 'error' ? (
      <Badge variant="danger">Problem</Badge>
    ) : null

  const serverButtons = hideServerActions
    ? []
    : list<CardAction>(card.actions).flatMap(action => {
        if (!isActionType(action.id)) return []
        const payload = payloadForCardAction(card, action.id)
        if (payload === undefined) return []
        const loading = ctx.pending?.cardId === card.id && ctx.pending.action === action.id
        return [
          <Button
            key={action.id}
            variant={BUTTON_VARIANT[action.variant] ?? 'secondary'}
            loading={loading}
            disabled={inert}
            onClick={() => void ctx.dispatch(card, action.id as ActionType, payload as never)}
          >
            {action.label}
          </Button>,
        ]
      })

  const actions =
    footer || serverButtons.length > 0 ? (
      <>
        {footer}
        {serverButtons}
      </>
    ) : undefined

  return (
    <ChatCard
      id={`card-${card.id}`}
      data-card-type={card.type}
      data-card-status={card.status}
      className={className}
      icon={icon}
      title={title ?? card.title}
      status={badge}
      superseded={superseded}
      actions={actions}
    >
      <fieldset disabled={inert} className="m-0 min-w-0 border-0 p-0">
        {children}
      </fieldset>
      {error ? (
        <p role="alert" className="mt-3 rounded-input border border-[color:var(--bad-border)] bg-[var(--bad-bg)] px-3 py-2 text-small text-[color:var(--bad-fg)]">
          {error}
        </p>
      ) : null}
    </ChatCard>
  )
}

/* ----------------------------------------------------------- shared bits */

const STATUS_BADGE: Partial<Record<FieldStatus, { label: string; variant: BadgeVariant }>> = {
  confirmed: { label: 'Confirmed', variant: 'success' },
  proposed: { label: 'Check', variant: 'warning' },
  engine: { label: 'Engine', variant: 'info' },
  default: { label: 'Default', variant: 'neutral' },
}

export const FIELD_STATUS_LABEL: Record<FieldStatus, string> = {
  confirmed: 'Confirmed',
  proposed: 'Proposed',
  engine: 'Engine',
  default: 'Default',
  empty: 'Empty',
}

/** Summary rows (protocol / budget summary cards) as a review table with status badges. */
export function SummaryTable({ rows, ariaLabel }: { rows: SummaryRow[]; ariaLabel: string }) {
  return (
    <ReviewTable
      ariaLabel={ariaLabel}
      rows={list(rows).map(r => {
        const b = STATUS_BADGE[r.status]
        return {
          label: r.label,
          value: r.value ?? <span className="font-normal text-fg-subtle">Not set</span>,
          highlight: r.needs_check,
          badge: r.needs_check ? <Badge variant="warning">Check</Badge> : b && r.status !== 'confirmed' ? <Badge variant={b.variant}>{b.label}</Badge> : undefined,
        }
      })}
    />
  )
}

/** A bulleted list of plain strings (notes, warnings, missing items). */
export function BulletList({ items, className }: { items: string[]; className?: string }) {
  const rows = list(items)
  if (rows.length === 0) return null
  return (
    <ul className={`m-0 list-disc space-y-1 pl-5 text-small text-fg-muted ${className ?? ''}`}>
      {rows.map((t, i) => (
        <li key={i}>{t}</li>
      ))}
    </ul>
  )
}

/** Inline tone box for in-card warnings and notes. */
export function Callout({ tone, children }: { tone: 'info' | 'warning' | 'danger' | 'neutral'; children: ReactNode }) {
  const cls = {
    info: 'border-[color:var(--info-border)] bg-[var(--info-bg)] text-[color:var(--info-fg)]',
    warning: 'border-[color:var(--warn-border)] bg-[var(--warn-bg)] text-[color:var(--warn-fg)]',
    danger: 'border-[color:var(--bad-border)] bg-[var(--bad-bg)] text-[color:var(--bad-fg)]',
    neutral: 'border-line bg-surface-muted text-fg-muted',
  }[tone]
  return (
    <div role={tone === 'danger' ? 'alert' : undefined} className={`rounded-input border px-3 py-2 text-small ${cls}`}>
      {children}
    </div>
  )
}
