/** Small formatting helpers shared by the shell and the pages under it. */

/** Postgres timestamps can arrive without a zone suffix; treat those as UTC. */
export function parseServerDate(iso: string): Date {
  const hasZone = /(Z|[+-]\d{2}:?\d{2})$/.test(iso)
  return new Date(hasZone ? iso : `${iso}Z`)
}

/** "Just now", "5 min ago", "3 h ago", "Yesterday", "4 days ago", else a date. */
export function relativeTime(iso: string, now: number = Date.now()): string {
  const then = parseServerDate(iso).getTime()
  if (!Number.isFinite(then)) return ''
  const seconds = Math.floor((now - then) / 1000)
  if (seconds < 45) return 'Just now'
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${Math.max(minutes, 1)} min ago`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours} h ago`
  const days = Math.floor(hours / 24)
  if (days === 1) return 'Yesterday'
  if (days < 30) return `${days} days ago`
  return new Date(then).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })
}

/** "adalimumab" -> "Adalimumab" (first letter only; INNs such as "insulin glargine" stay readable). */
export function capitalize(value: string): string {
  return value ? value.charAt(0).toUpperCase() + value.slice(1) : value
}

/** "Good morning" / "Good afternoon" / "Good evening" from the browser's local hour. */
export function greetingFor(hour: number): string {
  if (hour < 12) return 'Good morning'
  if (hour < 18) return 'Good afternoon'
  return 'Good evening'
}

/** First name when known, otherwise the first word of the email local part. */
export function displayNameFrom(firstName: string | null | undefined, email: string | null | undefined): string {
  const first = firstName?.trim()
  if (first) return first
  const local = (email ?? '').split('@')[0] ?? ''
  const word = local.split(/[._+\-\s]+/).filter(Boolean)[0] ?? ''
  return capitalize(word)
}

/** Org-initial for the workspace avatar. */
export function initialOf(name: string | null | undefined): string {
  const ch = Array.from((name ?? '').trim())[0]
  return ch ? ch.toUpperCase() : 'Q'
}

export const STUDY_TYPE_LABELS: Record<string, string> = {
  BE: 'Bioequivalence',
  biosimilar_pk: 'Biosimilar PK',
  food_effect: 'Food effect',
}
