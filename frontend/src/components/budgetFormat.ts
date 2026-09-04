/**
 * Tiny formatting helpers shared by the budgeting tool's components. No JSX
 * here — this file exists so BudgetList, BudgetProtocolPicker, BudgetEditor
 * and the line-item table all format money/quantities/dates identically.
 */

const moneyFormatterCache = new Map<string, Intl.NumberFormat>()

function moneyFormatter(currency: string): Intl.NumberFormat {
  let fmt = moneyFormatterCache.get(currency)
  if (!fmt) {
    fmt = new Intl.NumberFormat(undefined, { style: 'currency', currency, maximumFractionDigits: 2 })
    moneyFormatterCache.set(currency, fmt)
  }
  return fmt
}

/** Formats a money value for display, or an em dash for null/undefined. A
 *  null total and a $0 total mean different things — never collapse one into
 *  the other. */
export function formatMoney(value: number | null | undefined, currency: string): string {
  if (value === null || value === undefined) return '—'
  try {
    return moneyFormatter(currency).format(value)
  } catch {
    // Unrecognized/invalid currency code — fall back rather than throwing.
    return `${value.toLocaleString(undefined, { maximumFractionDigits: 2 })} ${currency}`
  }
}

/** Formats a quantity with up to 2 decimals and no trailing zeros; em dash
 *  for null/undefined. */
export function formatQty(n: number | null | undefined): string {
  if (n === null || n === undefined) return '—'
  return n.toLocaleString(undefined, { maximumFractionDigits: 2 })
}

/** Parses a money/quantity-ish input string ("$1,200.50", " 1200 ") into a
 *  number, stripping currency symbols/commas/spaces. Returns null for a
 *  blank or unparseable input — never throws, never returns NaN. */
export function parseMoneyInput(raw: string): number | null {
  const stripped = raw.replace(/[^0-9.\-]/g, '').trim()
  if (stripped === '' || stripped === '-' || stripped === '.') return null
  const n = Number(stripped)
  return Number.isFinite(n) ? n : null
}

/** Relative-date label ("Today" / "3 days ago" / a short date). Intentionally
 *  duplicated from ReportsView's own copy rather than imported from it — this
 *  file is the ONE shared copy for every budgeting-tool component. */
export function relativeDate(iso: string): string {
  const then = new Date(iso).getTime()
  const days = Math.floor((Date.now() - then) / 86_400_000)
  if (days <= 0) return 'Today'
  if (days === 1) return 'Yesterday'
  if (days < 30) return `${days} days ago`
  return new Date(iso).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })
}
