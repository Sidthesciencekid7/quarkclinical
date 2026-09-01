/** The "Quark AI" brand mark — an outer ring containing three small ringed
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
