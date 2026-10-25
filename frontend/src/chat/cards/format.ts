/**
 * Display helpers for card data. Formatting only: no card computes a value, it
 * shows the numbers the server's cards carry.
 */
import { formatMoney } from '../../components/budgetFormat'
import type { BudgetPreviewCardData } from '../types'

export { formatMoney }

/** `a ?? b ?? []` for a list field that an older/newer server might omit or null. */
export function list<T>(value: readonly T[] | null | undefined): T[] {
  return Array.isArray(value) ? (value as T[]) : []
}

export function capitalize(text: string): string {
  return text ? text[0].toUpperCase() + text.slice(1) : text
}

/** "2026-10-24T10:00:00Z" -> "24 Oct 2026" (or '' when unparseable). */
export function shortDate(iso: string | null | undefined): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  return d.toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' })
}

/** Month counts as "6" / "6.5" (no trailing zeros). */
export function formatMonths(n: number): string {
  return Number.isInteger(n) ? String(n) : String(Number(n.toFixed(1)))
}

/** The inline "here's what we'll build" hint of a budget preview, composed from
 *  the preview's own fields (moved out of the retired BudgetCreateView). */
export function previewHint(drugInn: string, preview: Pick<BudgetPreviewCardData, 'subject_profile' | 'subject_profile_basis' | 'defaults'>): string {
  const d = preview.defaults
  const label = drugInn.trim() ? drugInn.trim()[0].toUpperCase() + drugInn.trim().slice(1) : 'This drug'
  if (preview.subject_profile === 'patient') {
    const bits = [
      d.n_sites != null ? `~${d.n_sites} sites` : null,
      d.n_countries != null ? `${d.n_countries} countries` : null,
      d.project_duration_months != null ? `${formatMonths(d.project_duration_months)} months` : null,
    ]
      .filter(Boolean)
      .join(', ')
    return `${label}: ${preview.subject_profile_basis} A patient study budget${bits ? ` (${bits})` : ''}.`
  }
  const bits = [
    d.n_sites != null ? `${d.n_sites} site${d.n_sites === 1 ? '' : 's'}` : null,
    d.n_enrolled != null ? `${d.n_enrolled} subjects` : null,
    d.project_duration_months != null ? `${formatMonths(d.project_duration_months)} months` : null,
  ]
    .filter(Boolean)
    .join(', ')
  return `${label}: ${preview.subject_profile_basis} A healthy-volunteer budget${bits ? ` (${bits})` : ''}.`
}
