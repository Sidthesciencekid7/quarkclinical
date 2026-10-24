/**
 * Small inline illustrations for the Home tool tiles (QuarkClinical navy and
 * cyan on the grey tile). Decorative: every one is aria-hidden; the tile's
 * label carries the meaning. Colours are SVG attributes (not Tailwind classes).
 */
import type { ReactNode } from 'react'

const NAVY = '#0B1E3D'
const CYAN = '#06B6D4'
const CYAN_SOFT = '#CFFAFE'
const LINE = '#E4E4E7'
const MUTE = '#D4D4D8'

function Frame({ children }: { children: ReactNode }) {
  return (
    <svg
      viewBox="0 0 128 96"
      width="128"
      height="96"
      fill="none"
      aria-hidden="true"
      focusable="false"
      className="h-auto w-[68%] max-w-[176px] transition-transform duration-panel ease-out-soft group-hover:scale-[1.04] motion-reduce:transition-none motion-reduce:group-hover:scale-100"
    >
      {/* the paper */}
      <rect x="24" y="9" width="64" height="78" rx="9" fill="#fff" stroke={LINE} />
      {children}
    </svg>
  )
}

function Title() {
  return (
    <>
      <rect x="33" y="19" width="26" height="4.5" rx="2.25" fill={NAVY} />
      <rect x="33" y="27" width="40" height="3" rx="1.5" fill={MUTE} />
    </>
  )
}

/** Overlapping reference (navy) and biosimilar (cyan) concentration curves. */
export function BiosimilarIllustration() {
  return (
    <Frame>
      <Title />
      <path d="M33 38v33h46" stroke={MUTE} strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M34 70c6 0 10-24 18-26 8-2 14 16 27 24" stroke={NAVY} strokeWidth="2" strokeLinecap="round" />
      <path d="M34 70c6 0 11-20 19-22 8-2 14 14 26 22" stroke={CYAN} strokeWidth="2" strokeLinecap="round" />
      <circle cx="92" cy="71" r="15" fill={CYAN} />
      <circle cx="87.5" cy="71" r="5.6" stroke="#fff" strokeWidth="1.8" />
      <circle cx="96.5" cy="71" r="5.6" stroke="#fff" strokeWidth="1.8" />
    </Frame>
  )
}

/** A 90% confidence interval inside the 80-125% acceptance limits. */
export function BioequivalenceIllustration() {
  return (
    <Frame>
      <Title />
      <path d="M45 36v37M77 36v37" stroke="#A1A1AA" strokeWidth="1.2" strokeDasharray="2 3" />
      <path d="M52 47h18" stroke={NAVY} strokeWidth="2" strokeLinecap="round" />
      <rect x="57.5" y="43.5" width="7" height="7" rx="1.5" fill={NAVY} />
      <path d="M50 60h24" stroke={CYAN} strokeWidth="2" strokeLinecap="round" />
      <rect x="58" y="56.5" width="7" height="7" rx="1.5" fill={CYAN} />
      <path d="M40 72h42" stroke={MUTE} strokeWidth="1.2" strokeLinecap="round" />
      <circle cx="92" cy="71" r="15" fill={CYAN} />
      <g transform="rotate(-40 92 71)">
        <rect x="83.5" y="66" width="17" height="10" rx="5" fill="#fff" />
        <path d="M92 66v10" stroke={CYAN} strokeWidth="1.5" />
      </g>
    </Frame>
  )
}

/** Fasted (navy) versus fed (cyan, later and lower) curves, with a plate. */
export function FoodEffectIllustration() {
  return (
    <Frame>
      <Title />
      <path d="M33 38v33h46" stroke={MUTE} strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M34 70c5 0 8-26 15-27 8-1 15 17 30 26" stroke={NAVY} strokeWidth="2" strokeLinecap="round" />
      <path d="M34 70c7 0 13-14 22-15 8-1 14 8 23 15" stroke={CYAN} strokeWidth="2" strokeLinecap="round" strokeDasharray="1 4.2" />
      <path d="M34 70c7 0 13-14 22-15 8-1 14 8 23 15" stroke={CYAN} strokeWidth="2" strokeLinecap="round" opacity="0.55" />
      <circle cx="92" cy="71" r="15" fill={CYAN} />
      <circle cx="92" cy="71" r="6.2" stroke="#fff" strokeWidth="1.8" />
      <circle cx="92" cy="71" r="2.4" fill="#fff" />
      <path d="M81.5 64v14M102.5 64v14" stroke="#fff" strokeWidth="1.6" strokeLinecap="round" />
    </Frame>
  )
}

/** Cost lines with a total, and a coin. */
export function BudgetIllustration() {
  return (
    <Frame>
      <Title />
      <rect x="33" y="37" width="38" height="5" rx="2.5" fill={NAVY} />
      <rect x="33" y="46" width="28" height="5" rx="2.5" fill={CYAN} />
      <rect x="33" y="55" width="44" height="5" rx="2.5" fill={CYAN_SOFT} />
      <path d="M33 66h46" stroke={MUTE} strokeWidth="1.2" />
      <rect x="33" y="71" width="22" height="5" rx="2.5" fill={NAVY} />
      <circle cx="92" cy="71" r="15" fill={CYAN} />
      <path d="M92 62.5v17M96.2 66.5c-1-1.6-2.4-2.3-4.2-2.3-2.4 0-4 1.2-4 3 0 4.2 8.6 2 8.6 6.4 0 1.9-1.7 3.3-4.6 3.3-1.9 0-3.5-.8-4.5-2.2" stroke="#fff" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" />
    </Frame>
  )
}

/** Results bars and a verified tick. */
export function StudyReportIllustration() {
  return (
    <Frame>
      <Title />
      <path d="M33 72h46" stroke={MUTE} strokeWidth="1.2" strokeLinecap="round" />
      <rect x="37" y="56" width="8" height="16" rx="2" fill={CYAN_SOFT} />
      <rect x="49" y="46" width="8" height="26" rx="2" fill={CYAN} />
      <rect x="61" y="52" width="8" height="20" rx="2" fill={NAVY} />
      <rect x="73" y="40" width="6" height="32" rx="2" fill={CYAN_SOFT} />
      <circle cx="92" cy="71" r="15" fill={CYAN} />
      <path d="M85 71.5l5 5 9-10" stroke="#fff" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" />
    </Frame>
  )
}

/** Text lines with one highlighted, and a pencil. */
export function EditProtocolIllustration() {
  return (
    <Frame>
      <Title />
      <rect x="31" y="36" width="50" height="3.5" rx="1.75" fill={MUTE} />
      <rect x="29" y="44" width="54" height="9" rx="3" fill={CYAN_SOFT} />
      <rect x="33" y="47" width="40" height="3.5" rx="1.75" fill={CYAN} />
      <rect x="31" y="58" width="46" height="3.5" rx="1.75" fill={MUTE} />
      <rect x="31" y="66" width="30" height="3.5" rx="1.75" fill={MUTE} />
      <circle cx="92" cy="71" r="15" fill={CYAN} />
      <path d="M85.5 78l1.2-4.6 9.6-9.6a2 2 0 013 0l.9.9a2 2 0 010 3l-9.6 9.6-5.1 1.3z" fill="#fff" />
      <path d="M94.2 66l3.6 3.6" stroke={CYAN} strokeWidth="1.3" />
    </Frame>
  )
}
