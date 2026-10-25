/**
 * Tile illustrations for the product cards. Decorative (aria-hidden), drawn on
 * the near-black card with white-alpha strokes and the cyan accent.
 */
import type { ProductKey } from './content'

function Frame({ children }: { children: React.ReactNode }) {
  return (
    <svg
      aria-hidden
      focusable="false"
      viewBox="0 0 240 150"
      className="h-full w-full"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
    >
      {children}
    </svg>
  )
}

const PAGE = 'fill-white/[0.05] stroke-white/20'
const LINE = 'fill-white/[0.16]'
const LINE_STRONG = 'fill-white/30'

/** A protocol page with section lines, a locked-number chip and a check. */
function ProtocolIllustration() {
  return (
    <Frame>
      <rect x="52" y="14" width="112" height="124" rx="10" className={PAGE} strokeWidth="1.5" />
      <rect x="66" y="28" width="52" height="7" rx="3.5" className="fill-accent-300" />
      <rect x="66" y="44" width="84" height="5" rx="2.5" className={LINE} />
      <rect x="66" y="55" width="76" height="5" rx="2.5" className={LINE} />
      <rect x="66" y="66" width="80" height="5" rx="2.5" className={LINE} />
      <rect x="66" y="84" width="40" height="6" rx="3" className={LINE_STRONG} />
      <rect x="66" y="96" width="84" height="5" rx="2.5" className={LINE} />
      <rect x="66" y="107" width="70" height="5" rx="2.5" className={LINE} />
      <rect x="66" y="118" width="58" height="5" rx="2.5" className={LINE} />
      {/* locked-number chip */}
      <rect x="132" y="70" width="82" height="42" rx="12" className="fill-[#0a0a0c] stroke-white/25" strokeWidth="1.2" />
      <rect x="143" y="81" width="30" height="5" rx="2.5" className="fill-white" fillOpacity="0.5" />
      <rect x="143" y="92" width="52" height="9" rx="4.5" className="fill-white" />
      <circle cx="204" cy="36" r="14" className="fill-accent-300" />
      <path d="M197.5 36.5l4.2 4.2 7.3-8.2" className="stroke-ink-950" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" />
    </Frame>
  )
}

/** A priced line-item sheet with a total. */
function BudgetIllustration() {
  const rows = [28, 48, 68, 88]
  return (
    <Frame>
      <rect x="40" y="14" width="160" height="122" rx="12" className={PAGE} strokeWidth="1.5" />
      {rows.map((y, i) => (
        <g key={y}>
          <rect x="54" y={y} width={[58, 74, 50, 66][i]} height="6" rx="3" className={LINE_STRONG} />
          <rect x="54" y={y + 11} width={[84, 60, 92, 70][i]} height="4" rx="2" className={LINE} />
          <rect x="156" y={y + 1} width="30" height="8" rx="4" className={LINE} />
          <line x1="54" y1={y + 22} x2="186" y2={y + 22} className="stroke-white/10" strokeWidth="1.5" />
        </g>
      ))}
      <rect x="54" y="114" width="132" height="16" rx="8" className="fill-[#0a0a0c] stroke-white/25" strokeWidth="1.2" />
      <rect x="64" y="120" width="34" height="4" rx="2" className="fill-white" fillOpacity="0.5" />
      <rect x="150" y="119" width="26" height="6" rx="3" className="fill-white" />
      <circle cx="198" cy="30" r="15" className="fill-accent-300" />
      <circle cx="198" cy="30" r="9" className="stroke-ink-950" strokeWidth="2" />
      <path d="M198 25.5v9" className="stroke-ink-950" strokeWidth="2" strokeLinecap="round" />
    </Frame>
  )
}

/** A concentration-time curve with a confidence band and points. */
function ReportIllustration() {
  return (
    <Frame>
      <rect x="34" y="14" width="172" height="122" rx="12" className={PAGE} strokeWidth="1.5" />
      <path d="M52 112h136M52 112V30" className="stroke-white/25" strokeWidth="1.5" strokeLinecap="round" />
      <path
        d="M52 108 C 72 40, 98 34, 116 54 S 160 100, 188 106"
        className="stroke-accent-300"
        strokeWidth="3"
        strokeLinecap="round"
      />
      <path
        d="M52 110 C 74 56, 100 50, 118 68 S 162 104, 188 108"
        className="stroke-white/35"
        strokeWidth="2"
        strokeLinecap="round"
        strokeDasharray="4 5"
      />
      {[0, 1, 2, 3, 4].map(i => (
        <circle
          key={i}
          cx={64 + i * 28}
          cy={[84, 52, 62, 86, 100][i]}
          r="3.5"
          className="fill-[#0a0a0c] stroke-accent-300"
          strokeWidth="2"
        />
      ))}
      <rect x="150" y="26" width="42" height="14" rx="7" className="fill-[#0a0a0c] stroke-white/25" strokeWidth="1.2" />
      <rect x="159" y="31" width="24" height="4" rx="2" className="fill-white" />
    </Frame>
  )
}

export function ProductIllustration({ product }: { product: ProductKey }) {
  if (product === 'budget') return <BudgetIllustration />
  if (product === 'report') return <ReportIllustration />
  return <ProtocolIllustration />
}
