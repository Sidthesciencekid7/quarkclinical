/**
 * Human names for identifiers the server sends as codes. Only used where a card
 * has just the id (e.g. `guideline_version`); anything unknown is shown as-is.
 */
const GUIDELINE_NAMES: Record<string, string> = {
  'FDA-BE-2014': 'FDA Bioequivalence guidance (2014)',
  'FDA-BE-2003': 'FDA Bioequivalence guidance (2003, superseded)',
  'EMA-BE-2010': 'EMA Bioequivalence guideline (2010)',
  'ICH-M13A-2024': 'ICH M13A (2024)',
  'ICH-M11-2025': 'ICH M11 (2025)',
}

export function guidelineLabel(id: string | null | undefined): string {
  if (!id) return ''
  return GUIDELINE_NAMES[id] ?? id
}
