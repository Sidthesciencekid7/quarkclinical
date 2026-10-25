import { Badge, BookOpen, Card, ChevronDown } from '../ui'
import type { Budget, BudgetProvenanceTier, BudgetSourceRecord } from '../types'

interface Props {
  budget: Budget
  expanded: boolean
  onToggle: () => void
}

// Newest-tier-first, matching the provenance meter's ordering and colors.
const TIER_ORDER: BudgetProvenanceTier[] = ['published_rate', 'derived', 'bid_calibrated', 'estimate']

const TIER_LABEL: Record<BudgetProvenanceTier, string> = {
  published_rate: 'Published rate',
  derived: 'Derived',
  bid_calibrated: 'Bid-calibrated',
  estimate: 'Estimate',
}

const TIER_COPY: Record<BudgetProvenanceTier, string> = {
  published_rate: 'Read directly from an official fee schedule, manufacturer price list, government schedule or peer-reviewed benchmark.',
  derived: 'Triangulated by us from two or more published figures. Not itself a published benchmark.',
  bid_calibrated: 'A unit rate taken from a real CRO bid for a comparable study. A single vendor’s price, not a market benchmark.',
  estimate: 'A rate-card approximation. No public figure exists for this item; treat it as an assumption to be replaced by a quote.',
}

const TIER_DOT: Record<BudgetProvenanceTier, string> = {
  published_rate: 'bg-emerald-500',
  derived: 'bg-teal-500',
  bid_calibrated: 'bg-indigo-500',
  estimate: 'bg-slate-300',
}

function SourceCard({
  id,
  source,
  lineLabels,
}: {
  id: string
  source: BudgetSourceRecord
  lineLabels: string[]
}) {
  return (
    <div id={`source-${id}`} className="rounded-xl border border-line bg-surface px-4 py-3.5">
      <p className="text-body font-semibold text-fg">{source.figure}</p>
      <p className="mt-1.5 text-small text-fg-muted">{source.citation}</p>
      {source.url && (
        <a
          href={source.url}
          target="_blank"
          rel="noopener noreferrer"
          className="mt-1.5 block break-all text-small font-medium text-[color:var(--link)] underline-offset-2 hover:underline"
        >
          {source.url}
        </a>
      )}
      <p className="mt-1.5 text-small text-fg-subtle">Accessed {source.accessed}</p>
      {source.caveat && (
        <p className="mt-2 rounded-lg border border-[color:var(--warn-border)] bg-[var(--warn-bg)] px-2.5 py-2 text-small text-[color:var(--warn-fg)]">
          {source.caveat}
        </p>
      )}
      {lineLabels.length > 0 && (
        <div className="mt-2.5 border-t border-line pt-2.5">
          <p className="text-small text-fg-subtle">
            Backs {lineLabels.length} line item{lineLabels.length === 1 ? '' : 's'}: {lineLabels.join(', ')}
          </p>
        </div>
      )}
    </div>
  )
}

/** "Where did this number come from" — a citation list beside
 *  BudgetDerivationPanel, grouped newest-tier-first. Everything it needs is
 *  already in the budget payload (derivation.sources + derivation.lines) —
 *  no fetch. Renders nothing when the budget carries no sourced figures
 *  (every healthy-volunteer budget, and any patient budget with none yet). */
export default function BudgetSourcesPanel({ budget, expanded, onToggle }: Props) {
  const sources = budget.derivation?.sources
  const lines = budget.derivation?.lines ?? []
  if (!sources || Object.keys(sources).length === 0) return null

  const entries = Object.entries(sources)
  const byTier = TIER_ORDER.map(tier => ({
    tier,
    items: entries.filter(([, s]) => s.tier === tier),
  })).filter(g => g.items.length > 0)

  return (
    <Card padding="none" className="overflow-hidden">
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={expanded}
        className="flex w-full cursor-pointer items-center justify-between gap-3 px-5 py-4 text-left"
      >
        <div className="flex flex-wrap items-center gap-2">
          <BookOpen aria-hidden className="h-4 w-4 flex-none text-[color:var(--ok-fg)]" />
          <h3 className="text-h3 text-fg">Sources &amp; citations</h3>
          <Badge>{entries.length}</Badge>
        </div>
        <ChevronDown
          aria-hidden
          className={`h-4 w-4 flex-none text-fg-subtle transition-transform duration-fast ${expanded ? 'rotate-180' : ''}`}
        />
      </button>

      {expanded && (
        <div className="space-y-5 px-5 pb-5">
          {byTier.map(({ tier, items }) => (
            <div key={tier}>
              <div className="flex items-center gap-2 pb-1.5">
                <span aria-hidden className={`h-2 w-2 flex-none rounded-full ${TIER_DOT[tier]}`} />
                <h4 className="eyebrow">{TIER_LABEL[tier]}</h4>
                <span className="text-small tabular-nums text-fg-subtle">· {items.length}</span>
              </div>
              <p className="mb-2.5 text-small text-fg-subtle">{TIER_COPY[tier]}</p>
              <div className="space-y-2.5">
                {items.map(([id, source]) => (
                  <SourceCard
                    key={id}
                    id={id}
                    source={source}
                    lineLabels={lines.filter(l => l.source_id === id).map(l => l.label)}
                  />
                ))}
              </div>
            </div>
          ))}
        </div>
      )}
    </Card>
  )
}
