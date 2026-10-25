/**
 * The guide line above the composer.
 *
 * After a card click the server returns a deterministic `guide`; the SSE turn
 * stream carries none. So after a typed turn this derives a modest one from the
 * spec summary the stream does carry: what is still missing, or "ready". It
 * never states a number or an engine value, only which steps remain.
 */
import { STARTER_CHIPS } from './starters'
import type { Guide, SessionKind, SpecSummary } from './types'

function labelFor(spec: SpecSummary, path: string): string {
  return spec.fields.find(f => f.path === path)?.label ?? path.replace(/_/g, ' ')
}

export function deriveGuide(kind: SessionKind, spec: SpecSummary | null): Guide | null {
  if (!spec) return null
  if (kind === 'protocol_edit') {
    return {
      text: 'Tell me what to change: a section to rewrite, or an input such as the CV.',
      chips: STARTER_CHIPS.protocol_edit,
    }
  }
  if (spec.ready) {
    return kind === 'budget'
      ? { text: 'Everything needed is set. Adjust anything, or review the summary and create the budget.', chips: ['Show the summary'] }
      : { text: 'Everything is set. Review the summary and generate when you are ready.', chips: ['Show the summary'] }
  }
  const missing = spec.missing.map(p => labelFor(spec, p))
  const noDrug = spec.missing.some(p => p === 'drug' || p === 'drug_inn')
  return {
    text: missing.length ? `Still needed: ${missing.join(', ')}.` : '',
    chips: noDrug ? STARTER_CHIPS[kind] : [],
  }
}
