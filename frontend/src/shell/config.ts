/**
 * Shell-wide switches and link builders.
 *
 * CHAT_ENABLED is the ONE flag that decides where "start a study" links go:
 *  - true (now): the Home tiles, Pinned items, "New study", "Ask" and the Home
 *    composer open `/app/chat/new?kind=...&preset=...`.
 *  - false: they would open the classic wizards at `/app/protocol` and
 *    `/app/budgeting`. Those wizards were deleted when chat shipped (the two
 *    paths now redirect to the Protocols and Budgets lists), so leave this on.
 * Flip it here and every tile, pinned item and "New ..." button follows.
 */
export const CHAT_ENABLED: boolean = true

export type StudyKind = 'protocol' | 'budget'
export type StudyPreset = 'biosimilar_pk' | 'be' | 'food_effect' | 'price'

export const NEW_CHAT_PATH = '/app/chat/new'
export const PROTOCOL_WIZARD_PATH = '/app/protocol'
export const BUDGET_WIZARD_PATH = '/app/budgeting'

export const FEEDBACK_EMAIL = 'quark2027@gmail.com'
export const FEEDBACK_HREF = `mailto:${FEEDBACK_EMAIL}?subject=${encodeURIComponent('QuarkClinical feedback')}`

/** Where a "start a <kind> study" link goes right now. */
export function newStudyHref(kind: StudyKind, preset?: StudyPreset): string {
  if (CHAT_ENABLED) {
    const qs = new URLSearchParams({ kind })
    if (preset) qs.set('preset', preset)
    return `${NEW_CHAT_PATH}?${qs.toString()}`
  }
  const base = kind === 'protocol' ? PROTOCOL_WIZARD_PATH : BUDGET_WIZARD_PATH
  return preset ? `${base}?preset=${preset}` : base
}
