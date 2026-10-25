/**
 * Starter text for a new conversation: what a Home tile / pinned preset says on
 * the user's behalf, the suggestion chips of an empty conversation, and the
 * composer placeholders. All plain text; nothing here is a number or a claim.
 */
import type { SessionKind } from './types'

export type NewChatKind = 'protocol' | 'budget'

export interface PresetDef {
  kind: NewChatKind
  /** Sent as the first user message (stored without a model call, then answered). */
  text: string
  label: string
}

export const PRESETS: Record<string, PresetDef> = {
  biosimilar_pk: { kind: 'protocol', label: 'Biosimilar PK protocol', text: 'I want to set up a biosimilar PK similarity protocol.' },
  be: { kind: 'protocol', label: 'Bioequivalence protocol', text: 'I want to set up a bioequivalence protocol.' },
  food_effect: { kind: 'protocol', label: 'Food-effect protocol', text: 'I want to set up a food-effect protocol.' },
  price: { kind: 'budget', label: 'Price a study', text: 'I want to price a study.' },
}

export function presetFor(preset: string | null | undefined): PresetDef | null {
  if (!preset) return null
  return Object.prototype.hasOwnProperty.call(PRESETS, preset) ? PRESETS[preset] : null
}

export const STARTER_CHIPS: Record<SessionKind, string[]> = {
  protocol: [
    'Biosimilar PK study of adalimumab in healthy volunteers',
    'Bioequivalence study of metformin 850 mg tablets',
    'Food-effect study of ibuprofen',
  ],
  budget: [
    'Price a patient study for nivolumab',
    'Price a bioequivalence study for metformin',
    'Price a biosimilar PK study of adalimumab',
  ],
  protocol_edit: ['Show an overview', 'Shorten the adverse events section', 'Show the QC summary'],
}

export const KIND_LABELS: Record<SessionKind, string> = {
  protocol: 'Protocol',
  budget: 'Budget',
  protocol_edit: 'Protocol edit',
}

export const KIND_PLACEHOLDERS: Record<SessionKind, string> = {
  protocol: 'Describe your study, for example a biosimilar PK study of adalimumab in healthy volunteers',
  budget: 'Name the drug you want priced, for example a patient study for nivolumab',
  protocol_edit: 'Describe the change, for example shorten the adverse events section',
}

export const KIND_EMPTY_TITLES: Record<SessionKind, string> = {
  protocol: 'Set up a study protocol',
  budget: 'Price a study',
  protocol_edit: 'Edit this protocol with AI',
}

export const KIND_EMPTY_BODY: Record<SessionKind, string> = {
  protocol:
    'Describe the study in plain language. The assistant asks for what it needs, and every number comes from the engines and appears in a card you confirm. Nothing is generated until you click Generate.',
  budget:
    'Give a drug name and the engines draw up a priced CRO budget around real-bid calibration. Every number appears in a card; nothing is created until you click Create budget.',
  protocol_edit:
    'Ask for a rewrite of a section, or a change to an engine input such as the CV or the design. You review the differences before anything is applied.',
}

export function isSessionKind(value: unknown): value is SessionKind {
  return value === 'protocol' || value === 'budget' || value === 'protocol_edit'
}
