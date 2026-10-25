import { Component } from 'react'
import type { ComponentType, ErrorInfo, ReactNode } from 'react'
import { Info } from '../../ui'
import type { Card, CardType, NoticeCardData } from '../types'
import { Callout, CardFrame } from './CardFrame'
import { BudgetPreviewCard, BudgetReadyCard, BudgetSummaryCard } from './BudgetCards'
import { DesignDecisionsCard, GuidelineCard, IndicationCard, InteractionScreeningCard } from './DesignCards'
import { EditDiffCard, EditImpactCard, EditProgressCard, ProtocolOverviewCard } from './EditCards'
import { GenerationProgressCard, ProtocolReadyCard, ProtocolSummaryCard } from './GenerationCards'
import { CvOverrideWarningCard, DrugAddOfferCard, DrugMatchesCard, PopulationCard, StudyTypeCard } from './ProtocolSetupCards'

export { CardContext, useCardContext } from './CardContext'
export type { CardContextValue } from './CardContext'
export { payloadForCardAction, isActionType } from './payloads'

/* ------------------------------------------------------------------ notice */

export function NoticeCard({ card }: { card: Card }) {
  const d = card.data as unknown as NoticeCardData
  const tone = d.level === 'error' ? 'danger' : d.level === 'warning' ? 'warning' : 'info'
  return (
    <CardFrame card={card} icon={<Info />} title={card.title || 'Notice'} hideServerActions>
      <Callout tone={tone}>{d.text}</Callout>
    </CardFrame>
  )
}

/* ----------------------------------------------------------- generic fallback */

const MAX_GENERIC_ROWS = 12
const MAX_GENERIC_TEXT = 200

function shortValue(v: unknown): string | null {
  if (typeof v === 'string') return v.length > MAX_GENERIC_TEXT ? `${v.slice(0, MAX_GENERIC_TEXT)}…` : v
  if (typeof v === 'number' || typeof v === 'boolean') return String(v)
  if (Array.isArray(v)) return v.every(x => typeof x === 'string' || typeof x === 'number') ? v.slice(0, 8).join(', ') : `${v.length} items`
  return null
}

/**
 * The card for a type this build does not know (or whose data this build
 * cannot read): its title and its simple fields as plain text. It has no
 * buttons, because it cannot know what payload one would send. It renders
 * whatever it is given; it never throws.
 */
export function GenericCard({ card }: { card: Card }) {
  const data = card.data && typeof card.data === 'object' ? card.data : {}
  const rows = Object.entries(data)
    .flatMap(([k, v]) => {
      const text = shortValue(v)
      return text === null || text === '' ? [] : [[k.replace(/_/g, ' '), text] as const]
    })
    .slice(0, MAX_GENERIC_ROWS)
  return (
    <CardFrame card={card} icon={<Info />} title={card.title || 'Update'} hideServerActions>
      {rows.length > 0 ? (
        <dl className="m-0 space-y-1 text-small">
          {rows.map(([k, v]) => (
            <div key={k} className="flex gap-3">
              <dt className="w-32 flex-none capitalize text-fg-subtle">{k}</dt>
              <dd className="m-0 min-w-0 break-words text-fg">{v}</dd>
            </div>
          ))}
        </dl>
      ) : (
        <p className="text-small text-fg-muted">This update has no details to show in this version of the app.</p>
      )}
    </CardFrame>
  )
}

/* ---------------------------------------------------------------- registry */

/** One renderer per CardType. */
export const CARD_COMPONENTS: Record<CardType, ComponentType<{ card: Card }>> = {
  drug_matches: DrugMatchesCard,
  drug_add_offer: DrugAddOfferCard,
  study_type: StudyTypeCard,
  population: PopulationCard,
  cv_override_warning: CvOverrideWarningCard,
  interaction_screening: InteractionScreeningCard,
  design_decisions: DesignDecisionsCard,
  indication: IndicationCard,
  guideline: GuidelineCard,
  protocol_summary: ProtocolSummaryCard,
  generation_progress: GenerationProgressCard,
  protocol_ready: ProtocolReadyCard,
  budget_preview: BudgetPreviewCard,
  budget_summary: BudgetSummaryCard,
  budget_ready: BudgetReadyCard,
  protocol_overview: ProtocolOverviewCard,
  edit_impact: EditImpactCard,
  edit_progress: EditProgressCard,
  edit_diff: EditDiffCard,
  notice: NoticeCard,
}

interface BoundaryProps {
  card: Card
  children: ReactNode
}

/** A card whose data this build cannot read falls back to the generic card instead of taking the page down. */
class CardBoundary extends Component<BoundaryProps, { failed: boolean }> {
  state = { failed: false }

  static getDerivedStateFromError() {
    return { failed: true }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.warn(`[chat] card ${this.props.card.type} failed to render`, error, info.componentStack)
  }

  componentDidUpdate(prev: BoundaryProps) {
    // New data may be readable: try the real renderer again.
    if (this.state.failed && prev.card.data !== this.props.card.data) this.setState({ failed: false })
  }

  render() {
    if (this.state.failed) return <GenericCard card={this.props.card} />
    return this.props.children
  }
}

/** Render one card: the component for its type, the generic card for an unknown type, and the generic card if the real one throws. */
export function CardRenderer({ card }: { card: Card }) {
  const Renderer = (Object.prototype.hasOwnProperty.call(CARD_COMPONENTS, card.type) ? CARD_COMPONENTS[card.type] : null) ?? GenericCard
  return (
    <CardBoundary card={card}>
      <Renderer card={card} />
    </CardBoundary>
  )
}
