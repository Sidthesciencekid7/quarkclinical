/**
 * The payload each card button submits, fixed by the action type and the
 * card's own data (see the "Button payloads" header in
 * backend/services/chat/contract.py). The card components never invent a
 * payload; they ask this table. Pure, so it is unit-tested.
 */
import { ACTION_SESSION_KINDS } from '../types'
import type {
  ActionType,
  Card,
  CvOverrideWarningCardData,
  DrugAddOfferCardData,
  EditDiffCardData,
  EditImpactCardData,
  PopulationCardData,
} from '../types'

export function isActionType(id: string): id is ActionType {
  return Object.prototype.hasOwnProperty.call(ACTION_SESSION_KINDS, id)
}

function str(v: unknown): string | null {
  return typeof v === 'string' && v !== '' ? v : null
}

/**
 * The payload for `card`'s server-declared button `actionId`, or `undefined`
 * when this build does not know how to build one (the button is then not
 * offered rather than sending something the server would reject).
 */
export function payloadForCardAction(card: Card, actionId: string): Record<string, unknown> | undefined {
  switch (actionId) {
    case 'add_drug': {
      const inn = str((card.data as unknown as DrugAddOfferCardData).inn)
      return inn ? { inn } : undefined
    }
    case 'update_population': {
      if (card.type === 'cv_override_warning') {
        const patch = (card.data as unknown as CvOverrideWarningCardData).clear_patch
        return patch && typeof patch === 'object' ? { patch } : undefined
      }
      if (card.type === 'population') {
        const patch = (card.data as unknown as PopulationCardData).confirm_patch
        return patch && typeof patch === 'object' ? { patch } : undefined
      }
      return undefined
    }
    case 'confirm_design':
    case 'generate_protocol':
    case 'create_budget':
      return {}
    case 'set_indication':
      // The indication card's button is "Let the engine choose": clear the user-supplied indication.
      return card.type === 'indication' ? { text: null } : undefined
    case 'confirm_edit':
    case 'discard_edit':
    case 'apply_edit': {
      const id = str((card.data as unknown as EditImpactCardData | EditDiffCardData).proposal_id)
      return id ? { proposal_id: id } : undefined
    }
    case 'link_protocol':
      // A server-declared link_protocol button means "unlink"; picking a protocol is the card's own picker.
      return card.type === 'budget_summary' ? { protocol_id: null } : undefined
    default:
      return undefined
  }
}
