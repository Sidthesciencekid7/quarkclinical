import type { Budget, BudgetProvenanceTier, BudgetSourceRecord } from '../types'

interface Props {
  budget: Budget
  expanded: boolean
  onToggle: () => void
}

function ChevronIcon({ open }: { open: boolean }) {
  return (
    <svg width="14" height="14" viewBox="0 0 20 20" fill="none" className={`transition-transform flex-shrink-0 ${open ? 'rotate-180' : ''}`}>
      <path d="M5 8l5 5 5-5" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}
function BookIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 20 20" fill="none" className="flex-shrink-0 text-emerald-600">
      <path d="M4 4.5A1.5 1.5 0 015.5 3H10v14H5.5A1.5 1.5 0 014 15.5v-11z" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
      <path d="M16 4.5A1.5 1.5 0 0014.5 3H10v14h4.5a1.5 1.5 0 001.5-1.5v-11z" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
    </svg>
  )
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
    <div id={`source-${id}`} className="rounded-xl border border-slate-200 bg-white px-4 py-3.5">
      <p className="text-sm font-bold text-slate-900">{source.figure}</p>
      <p className="text-xs text-slate-600 leading-relaxed mt-1.5">{source.citation}</p>
      {source.url && (
        <a
          href={source.url}
          target="_blank"
          rel="noopener noreferrer"
          className="block text-xs font-semibold text-cyan-700 hover:text-cyan-800 break-all mt-1.5"
        >
          {source.url}
        </a>
      )}
      <p className="text-[11px] text-slate-400 mt-1.5">Accessed {source.accessed}</p>
      {source.caveat && (
        <p className="text-[11px] text-amber-800 bg-amber-50 border border-amber-200 rounded-lg px-2.5 py-2 mt-2 leading-relaxed">
          {source.caveat}
        </p>
      )}
      {lineLabels.length > 0 && (
        <div className="mt-2.5 pt-2.5 border-t border-slate-100">
          <p className="text-[11px] text-slate-400">
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
    <div className="card overflow-hidden">
      <button onClick={onToggle} className="w-full flex items-center justify-between gap-3 px-5 py-4 text-left cursor-pointer">
        <div className="flex items-center gap-2 flex-wrap">
          <BookIcon />
          <h3 className="text-sm font-bold text-slate-900">Sources &amp; citations</h3>
          <span className="text-[10px] font-bold text-slate-500 bg-slate-100 border border-slate-200 px-1.5 py-0.5 rounded-full whitespace-nowrap">
            {entries.length}
          </span>
        </div>
        <ChevronIcon open={expanded} />
      </button>

      {expanded && (
        <div className="px-5 pb-5 space-y-5">
          {byTier.map(({ tier, items }) => (
            <div key={tier}>
              <div className="flex items-center gap-2 pb-1.5">
                <span className={`w-2 h-2 rounded-full flex-shrink-0 ${TIER_DOT[tier]}`} />
                <h4 className="text-[11px] font-bold text-slate-500 uppercase tracking-wider">{TIER_LABEL[tier]}</h4>
                <span className="text-[11px] text-slate-400">· {items.length}</span>
              </div>
              <p className="text-[11px] text-slate-400 mb-2.5">{TIER_COPY[tier]}</p>
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
    </div>
  )
}
