/** The QuarkClinical brand mark — an outer ring containing three small ringed
 *  particles in a triangular arrangement (three quarks). Stroke-only, no
 *  fill, so it inherits color from `stroke`/`currentColor` like every other
 *  icon in this codebase. Used in every page header/logo across the site. */
export default function QuarkLogo({
  size = 20,
  stroke = 'currentColor',
  className,
}: {
  size?: number
  stroke?: string
  className?: string
}) {
  return (
    <svg width={size} height={size} viewBox="0 0 20 20" fill="none" className={className}>
      <circle cx="10" cy="10" r="8.2" stroke={stroke} strokeWidth="1.4" />
      <circle cx="10" cy="5.85" r="1.5" stroke={stroke} strokeWidth="1.15" />
      <circle cx="10" cy="5.85" r="0.6" stroke={stroke} strokeWidth="1.15" />
      <circle cx="13.6" cy="12.08" r="1.5" stroke={stroke} strokeWidth="1.15" />
      <circle cx="13.6" cy="12.08" r="0.6" stroke={stroke} strokeWidth="1.15" />
      <circle cx="6.4" cy="12.08" r="1.5" stroke={stroke} strokeWidth="1.15" />
      <circle cx="6.4" cy="12.08" r="0.6" stroke={stroke} strokeWidth="1.15" />
    </svg>
  )
}

/** "Quark" + cyan "Clinical". `tone` picks the colour pair for the surface it
 *  sits on: `light` (white/app surfaces) uses ink-950 + cyan-700 (5.4:1 on
 *  white); `dark` (navy landing) uses white + cyan-300. Pass `showMark` to put
 *  the ring mark in front. Reads as one accessible name: "QuarkClinical". */
export function Wordmark({
  tone = 'light',
  size = 'md',
  showMark = false,
  className,
}: {
  tone?: 'light' | 'dark'
  size?: 'sm' | 'md' | 'lg'
  showMark?: boolean
  className?: string
}) {
  const text = size === 'sm' ? 'text-[15px]' : size === 'lg' ? 'text-[22px]' : 'text-[18px]'
  const quark = tone === 'dark' ? 'text-white' : 'text-ink-950'
  const clinical = tone === 'dark' ? 'text-accent-300' : 'text-accent-700'
  const markSize = size === 'sm' ? 18 : size === 'lg' ? 26 : 22
  return (
    <span
      className={`inline-flex items-center gap-2 font-semibold leading-none tracking-[-0.02em] ${text} ${className ?? ''}`}
      aria-label="QuarkClinical"
      role="img"
    >
      {showMark ? <QuarkLogo size={markSize} className={quark} /> : null}
      <span aria-hidden="true">
        <span className={quark}>Quark</span>
        <span className={clinical}>Clinical</span>
      </span>
    </span>
  )
}
