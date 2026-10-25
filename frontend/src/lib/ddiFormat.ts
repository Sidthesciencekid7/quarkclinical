/**
 * Interaction-screening text helpers (moved out of PopulationBuilder).
 *
 * A screening row becomes ONE exclusion-criterion sentence, because that is
 * what the population's exclusion list carries into the protocol. The engine's
 * own sentence is used verbatim when it supplies one, so the wording the
 * protocol states is the wording the engine wrote (including its washout).
 *
 * Works on both shapes of a row: the wizard-era `DdiRow` (types.ts) and the
 * chat card's `DdiRowData` (chat/types.ts); `criterionText` prefers the
 * server-built `criterion_text` of the latter.
 */

/** Display names for the pathway keys the screening engine emits. */
export const PATHWAY_LABELS: Record<string, string> = {
  cyp3a4: 'CYP3A4',
  cyp2d6: 'CYP2D6',
  cyp2c19: 'CYP2C19',
  cyp2c9: 'CYP2C9',
  cyp2c8: 'CYP2C8',
  cyp1a2: 'CYP1A2',
  cyp2b6: 'CYP2B6',
  ugt: 'UGT',
  pgp: 'P-gp',
  bcrp: 'BCRP',
  oatp: 'OATP',
  oct_mate: 'OCT/MATE',
}

export function pathwayLabel(key: string): string {
  return PATHWAY_LABELS[key] ?? key.toUpperCase()
}

/** The fields both row shapes share. */
export interface DdiRowLike {
  key?: string
  label: string
  status?: string
  pathway?: string | null
  examples?: string[] | null
  washout_days?: number | null
  washout_basis?: string | null
  history_exclusion?: boolean | null
  rationale?: string | null
  conditions?: string | null
  escalated_by_nti?: boolean | null
  assumption?: string | null
  /** Wizard shape: `{quote}`; chat shape uses `evidence_quote`. */
  evidence?: { quote?: string | null } | null
  evidence_quote?: string | null
  /** Wizard shape: optional server sentence. */
  criterion?: string | null
  /** Chat shape: the server-built sentence. */
  criterion_text?: string | null
}

export function formatDays(days: number): string {
  const rounded = Number.isInteger(days) ? days : Number(days.toFixed(1))
  return `${rounded} day${rounded === 1 ? '' : 's'}`
}

export function criterionText(row: DdiRowLike): string {
  const server = row.criterion_text?.trim() || row.criterion?.trim()
  if (server) return server

  const examples = (row.examples ?? []).filter(Boolean).slice(0, 3)
  const head = examples.length ? `${row.label} (e.g. ${examples.join(', ')})` : row.label

  if (row.history_exclusion) return `${head}: excluded on history (cannot be washed out)`
  if (typeof row.washout_days === 'number' && row.washout_days > 0) {
    return `${head} within ${formatDays(row.washout_days)} before first dose`
  }
  if (row.conditions) return `${head}: ${row.conditions}`
  return head
}

/** Everything the engine knows about a row: the provenance a reviewer needs to
 *  accept or reject the criterion. One fact per line. */
export function criterionLines(row: DdiRowLike): string[] {
  const lines: string[] = []
  if (row.pathway) lines.push(`Pathway: ${PATHWAY_LABELS[row.pathway] ?? row.pathway}`)
  if (row.rationale) lines.push(row.rationale)
  if (row.washout_basis) lines.push(`Washout basis: ${row.washout_basis}`)
  if (row.conditions) lines.push(`Conditions: ${row.conditions}`)
  if (row.escalated_by_nti) lines.push('Escalated because the drug is narrow-therapeutic-index.')
  if (row.assumption) lines.push(`Assumption: ${row.assumption}`)
  const quote = row.evidence_quote ?? row.evidence?.quote
  if (quote) lines.push(`Label evidence: "${quote}"`)
  return lines
}

export function criterionTooltip(row: DdiRowLike): string {
  return criterionLines(row).join('\n')
}
