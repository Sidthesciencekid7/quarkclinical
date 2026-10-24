/**
 * TypeScript mirror of the chat wire contract: backend/services/chat/contract.py
 * (docs/redesign-chat-plan.md, B1-B9). The two files must change together;
 * backend/tests/test_chat_contract.py fails if the string-literal unions below
 * drift from the Python enums, so keep each union on one `| 'value'` per line
 * with no comments inside it.
 *
 * Server -> client shapes list every field the server always sends (Python
 * defaults are serialised), so they are non-optional here. Client -> server
 * request shapes mark the fields the server defaults as optional.
 */
import type { StudyType } from '../types'

/* ---------------------------------------------------------------- enums */

export type SessionKind =
  | 'protocol'
  | 'budget'
  | 'protocol_edit'

export type SessionStatus =
  | 'active'
  | 'completed'
  | 'archived'

export type MessageRole =
  | 'user'
  | 'assistant'
  | 'tool_results'
  | 'ui_action'
  | 'system_note'

export type CardType =
  | 'drug_matches'
  | 'drug_add_offer'
  | 'study_type'
  | 'population'
  | 'cv_override_warning'
  | 'interaction_screening'
  | 'design_decisions'
  | 'indication'
  | 'guideline'
  | 'protocol_summary'
  | 'generation_progress'
  | 'protocol_ready'
  | 'budget_preview'
  | 'budget_summary'
  | 'budget_ready'
  | 'protocol_overview'
  | 'edit_impact'
  | 'edit_progress'
  | 'edit_diff'
  | 'notice'

export type CardStatus =
  | 'active'
  | 'superseded'
  | 'done'
  | 'error'

export type ActionType =
  | 'select_drug'
  | 'add_drug'
  | 'set_study_type'
  | 'set_guideline'
  | 'update_population'
  | 'toggle_exclusion'
  | 'set_design_decision'
  | 'set_indication'
  | 'confirm_design'
  | 'generate_protocol'
  | 'set_budget_fields'
  | 'link_protocol'
  | 'create_budget'
  | 'confirm_edit'
  | 'apply_edit'
  | 'discard_edit'

export type FieldStatus =
  | 'empty'
  | 'default'
  | 'engine'
  | 'proposed'
  | 'confirmed'

export type FieldSource =
  | 'default'
  | 'engine'
  | 'user_click'
  | 'user_text'

export type ProposalKind =
  | 'prose'
  | 'engine'

export type ProposalStatus =
  | 'proposed'
  | 'generating'
  | 'ready'
  | 'applied'
  | 'discarded'
  | 'failed'
  | 'stale'

export type ChatErrorCode =
  | 'busy'
  | 'stale_spec'
  | 'proposal_stale'
  | 'license_required'
  | 'rate_limited'
  | 'daily_cap'
  | 'session_cap'
  | 'forbidden'
  | 'not_found'
  | 'validation'
  | 'invalid_action'
  | 'model_error'
  | 'internal'
  | 'stream_interrupted'

export type SseEventName =
  | 'turn_start'
  | 'text_delta'
  | 'tool_start'
  | 'card'
  | 'spec'
  | 'notice'
  | 'turn_end'
  | 'error'
  | 'tick'

export type CardActionVariant =
  | 'primary'
  | 'secondary'
  | 'ghost'
  | 'danger'

export type NoticeLevel =
  | 'info'
  | 'warning'
  | 'error'

/** Statuses a client may PATCH a session to ('completed' is server-controlled). */
export type PatchableSessionStatus = 'active' | 'archived'

/* --------------------------------------------------------------- limits */

export const MAX_TURN_TEXT = 4000
export const MAX_INITIAL_MESSAGE = 4000
export const MAX_TITLE = 200
export const MAX_CLIENT_TURN_ID = 64
export const MAX_INDICATION_LENGTH = 200
export const MAX_EXCLUSION_TEXT = 300
export const DEFAULT_LIST_LIMIT = 20
export const MAX_LIST_LIMIT = 100
export const DEFAULT_GUIDELINE_VERSION = 'FDA-BE-2014'

/* ---------------------------------------------------------------- cards */

export interface CardAction {
  /** The ActionType value to submit when the button is clicked. */
  id: string
  label: string
  variant: CardActionVariant
}

export interface Card {
  id: string
  type: CardType
  status: CardStatus
  /** Draft-spec version the card was built from. */
  spec_version: number
  title: string
  /** Plain JSON; shape depends on `type` (see backend services/chat/cards.py). */
  data: Record<string, unknown>
  actions: CardAction[]
}

/* ----------------------------------------------------------- spec panel */

export interface SpecField {
  path: string
  label: string
  display_value: string | null
  status: FieldStatus
  /** null while the field is still empty. */
  source: FieldSource | null
}

export interface SpecSummary {
  kind: SessionKind
  spec_version: number
  /** True when Generate / Create is available. */
  ready: boolean
  /** `SpecField.path` of every field still required. */
  missing: string[]
  fields: SpecField[]
}

export interface Guide {
  text: string
  chips: string[]
}

export interface MessageFlags {
  /** Numbers in the assistant text not found in any tool result / spec / card. */
  unverified_numbers: string[]
}

/* ------------------------------------------------- sessions / messages */

export interface CreateChatRequest {
  kind: SessionKind
  /** protocol_edit: required target. budget: optional protocol to link. protocol: not allowed. */
  protocol_id?: string | null
  initial_message?: string | null
}

export interface ChatSessionRead {
  id: string
  kind: SessionKind
  status: SessionStatus
  title: string | null
  spec_version: number
  protocol_id: string | null
  budget_id: string | null
  /** ISO 8601, always UTC ('Z'). */
  created_at: string
  updated_at: string
  last_activity_at: string
}

export interface ChatSessionListItem {
  id: string
  kind: SessionKind
  status: SessionStatus
  title: string | null
  protocol_id: string | null
  budget_id: string | null
  created_at: string
  last_activity_at: string
}

export interface ChatMessageDisplay {
  id: string
  seq: number
  role: MessageRole
  text: string
  card_ids: string[]
  flags: MessageFlags
  created_at: string
}

export interface ChatSessionDetail {
  session: ChatSessionRead
  messages: ChatMessageDisplay[]
  cards: Card[]
  spec: SpecSummary
  /** True when the last message is an unanswered user message: POST a turn with resume=true. */
  awaiting_reply: boolean
}

/** Send exactly one of `text` or `resume: true`. */
export interface TurnRequest {
  text?: string | null
  resume?: boolean
  /** Idempotency key: 1-64 chars of [A-Za-z0-9._:-]. */
  client_turn_id: string
}

export interface PatchChatRequest {
  title?: string | null
  status?: PatchableSessionStatus | null
}

/* -------------------------------------------------------------- actions */

export interface ActionPayloadMap {
  select_drug: { drug_id: string }
  add_drug: { inn: string }
  set_study_type: { study_type: StudyType }
  set_guideline: { guideline_version: string }
  update_population: { patch: Record<string, unknown> }
  toggle_exclusion: { text: string; on: boolean }
  set_design_decision: { key: string; value: string }
  /** `text: null` clears the user-supplied indication. */
  set_indication: { text: string | null }
  confirm_design: Record<string, never>
  generate_protocol: Record<string, never>
  set_budget_fields: { patch: Record<string, unknown> }
  /** `protocol_id: null` unlinks. */
  link_protocol: { protocol_id: string | null }
  create_budget: Record<string, never>
  confirm_edit: { proposal_id: string }
  apply_edit: { proposal_id: string }
  discard_edit: { proposal_id: string }
}

export interface ActionRequest<A extends ActionType = ActionType> {
  card_id: string
  action: A
  payload: ActionPayloadMap[A]
  /** Spec version the client last saw; a mismatch is 409 `stale_spec`. */
  spec_version: number
}

/** Distributes over ActionType so `action` and `payload` stay correlated. */
export type AnyActionRequest = { [A in ActionType]: ActionRequest<A> }[ActionType]

export interface ActionResponse {
  spec_version: number
  spec: SpecSummary
  /** Cards created or changed by the action; upsert by id. */
  cards: Card[]
  guide: Guide
}

/** Which session kinds may submit each action (mirrors ACTION_SESSION_KINDS). */
export const ACTION_SESSION_KINDS: Readonly<Record<ActionType, readonly SessionKind[]>> = {
  select_drug: ['protocol'],
  add_drug: ['protocol'],
  set_study_type: ['protocol'],
  set_guideline: ['protocol'],
  update_population: ['protocol'],
  toggle_exclusion: ['protocol'],
  set_design_decision: ['protocol'],
  set_indication: ['protocol'],
  confirm_design: ['protocol'],
  generate_protocol: ['protocol'],
  set_budget_fields: ['budget'],
  link_protocol: ['budget'],
  create_budget: ['budget'],
  confirm_edit: ['protocol_edit'],
  apply_edit: ['protocol_edit'],
  discard_edit: ['protocol_edit'],
}

/* ------------------------------------------------------------ proposals */

export interface SectionDiffItem {
  section_name: string
  display_name: string
  status: 'unchanged' | 'modified' | 'added' | 'removed'
  old_content: string | null
  new_content: string | null
  diff_lines: string[]
  word_count_delta: number
}

export interface ProposalRead {
  id: string
  protocol_id: string
  session_id: string | null
  kind: ProposalKind
  status: ProposalStatus
  base_version: number
  applied_version: number | null
  request: Record<string, unknown>
  /** Populated once status === 'ready'. */
  diff: SectionDiffItem[]
  qc: Record<string, unknown> | null
  facts_before: Record<string, unknown> | null
  facts_after: Record<string, unknown> | null
  error: string | null
  started_at: string | null
  created_at: string
  updated_at: string
}

/* ------------------------------------------------------------ SSE events */

export interface TurnStartEvent { turn_id: string }
export interface TextDeltaEvent { text: string }
export interface ToolStartEvent { tool: string; label: string }
export interface SpecEvent { spec_version: number; summary: SpecSummary }
export interface NoticeEvent { level: NoticeLevel; text: string }
export interface TurnEndEvent {
  message_id: string
  /** The model's stop reason verbatim (end_turn, tool_use, max_tokens, pause_turn, refusal, ...) or a server value such as 'iteration_limit'. */
  stop_reason: string
  flags: MessageFlags
}
export interface ErrorEvent {
  /** A ChatErrorCode value (typed string so an unknown code never breaks the error path). */
  code: string
  message: string
  retryable: boolean
}
export interface StreamCheckTick {
  seq: number
  total: number
  elapsed_ms: number
}

/** Event name -> payload for the turn stream. The `card` payload is a bare Card. */
export interface TurnEventMap {
  turn_start: TurnStartEvent
  text_delta: TextDeltaEvent
  tool_start: ToolStartEvent
  card: Card
  spec: SpecEvent
  notice: NoticeEvent
  turn_end: TurnEndEvent
  error: ErrorEvent
}

export type TurnEventName = keyof TurnEventMap

/** Discriminated union delivered to `streamTurn`'s `onEvent`. */
export type ChatStreamEvent = { [K in TurnEventName]: { event: K; data: TurnEventMap[K] } }[TurnEventName]

/** The object form of an HTTP error `detail` on chat routes. */
export interface ErrorDetail {
  code: string
  message: string
}
