/**
 * Landing -> app handoff helpers (pure, so they are easy to test).
 *
 * The visitor's description is parked in localStorage by savePendingPrompt()
 * (src/lib/pendingPrompt.ts) and picked up by /app/chat/new?from=landing. The
 * launch target below carries only a fixed `kind` token, NEVER the text: the
 * description is user-typed, may be sensitive, and must not reach history,
 * referrers, analytics or server logs.
 */
import type { PendingPromptKind } from '../lib/pendingPrompt'

export function launchTargetFor(kind: PendingPromptKind): string {
  return `/app/chat/new?from=landing&kind=${kind}`
}

export type LandingSectionId = 'top' | 'products' | 'chat' | 'why' | 'how' | 'pricing' | 'faq' | 'cta'

/** The nav item each section belongs to ('' = none highlighted). */
export const SECTION_TO_NAV: Record<LandingSectionId, string> = {
  top: '',
  products: 'products',
  chat: 'products',
  why: 'products',
  how: 'how',
  pricing: 'pricing',
  faq: 'faq',
  cta: '',
}

export const SECTION_ORDER: LandingSectionId[] = ['top', 'products', 'chat', 'why', 'how', 'pricing', 'faq', 'cta']

/**
 * Scroll-spy decision: given each section's current top offset (px from the
 * viewport top, `null` when the section is missing) and the reference line,
 * the active section is the LAST one whose top has crossed the line.
 */
export function pickActiveSection(
  tops: Partial<Record<LandingSectionId, number | null>>,
  line: number,
): LandingSectionId | null {
  let active: LandingSectionId | null = null
  for (const id of SECTION_ORDER) {
    const top = tops[id]
    if (typeof top === 'number' && top <= line) active = id
  }
  return active
}
