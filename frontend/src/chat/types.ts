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

/* =====================================================================
 * Card `data` models (backend/services/chat/contract.py, "CARD DATA MODELS").
 *
 * `Card.data` is plain JSON whose shape depends on `Card.type`. Optional
 * fields are always PRESENT in the JSON (`null` when unset) because the
 * builders dump without `exclude_none`; the card components still read every
 * list defensively (`?? []`) because a card written by an older or newer
 * build must never crash the page.
 * ===================================================================== */

export type Modality = 'biologic' | 'small_molecule' | 'unknown'

/** A label/value pair (summary rows, locked facts, estimates). */
export interface FactRow {
  label: string
  value: string
}

/** One row of a protocol / budget summary card. */
export interface SummaryRow {
  /** SpecField.path the row summarises. */
  path: string
  label: string
  value: string | null
  status: FieldStatus
  source: FieldSource | null
  /** True while the value is only `proposed`: the card highlights it. */
  needs_check: boolean
}

/* drug_matches / drug_add_offer */

export interface DrugMatchItem {
  id: string
  inn: string
  brand_names: string[]
  modality: Modality
  /** Study types this drug may run; empty when the modality is unknown (unrestricted). */
  allowed_study_types: string[]
}

export interface DrugMatchesCardData {
  query: string
  matches: DrugMatchItem[]
  truncated: boolean
  selected_drug_id: string | null
}

export interface DrugAddOfferCardData {
  query: string
  /** Normalised INN the add_drug action sends; null when `query` is not a valid INN. */
  inn: string | null
  valid: boolean
  message: string
  modality: Modality
  allowed_study_types: string[]
}

/* study_type */

export interface StudyTypeOption {
  value: StudyType
  label: string
  allowed: boolean
  /** Why a disallowed option is disabled (the server's own gate wording). */
  reason: string | null
}

export interface StudyTypeCardData {
  drug_inn: string | null
  modality: Modality
  options: StudyTypeOption[]
  selected: StudyType | null
  /** Set when an attempted choice was refused. */
  gate_message: string | null
}

/* population / cv_override_warning */

export interface PopulationValues {
  subject_type: 'healthy' | 'patient'
  age_min: number
  age_max: number
  sex: 'male' | 'female' | 'both'
  weight_min: number
  weight_max: number
  exclusion_presets: string[]
  cv_intra: number | null
  dose_strength: string | null
  reference_product: string | null
  is_nti: boolean | null
  additional_pk_timepoints_hours: string | null
  additional_immunogenicity_timepoints_months: string | null
}

export interface PopulationRanges {
  age_min: number
  age_max: number
  weight_min: number
  weight_max: number
  cv_min: number
  cv_max: number
}

export interface PopulationCardData {
  drug_inn: string | null
  study_type: StudyType | null
  values: PopulationValues
  ranges: PopulationRanges
  /** Drug-agnostic exclusion criteria offered as toggles. */
  generic_exclusion_options: string[]
  /** `SpecField.path` -> FieldStatus value, for the fields shown. */
  field_status: Record<string, string>
  /** Patch for the "Looks good" quick-confirm (an `update_population` payload). */
  confirm_patch: Record<string, unknown>
}

export interface CvOverrideWarningCardData {
  cv_intra: number
  message: string
  consequences: string[]
  /** `update_population` patch that removes the override: `{"cv_intra": null}`. */
  clear_patch: Record<string, unknown>
}

/* interaction_screening */

export interface DdiRowData {
  key: string
  label: string
  /** "prohibited" | "restricted" | "permitted" (open string: engine-owned). */
  status: string
  pathway: string | null
  examples: string[]
  washout_days: number | null
  washout_basis: string | null
  history_exclusion: boolean
  rationale: string | null
  conditions: string | null
  escalated_by_nti: boolean
  assumption: string | null
  /** Matched DailyMed label text backing the row (display as quoted data). */
  evidence_quote: string | null
  /** The exclusion-criterion sentence the toggle adds. */
  criterion_text: string
  /** True when `criterion_text` is currently in the spec's exclusion list. */
  selected: boolean
}

export interface InteractionScreeningCardData {
  drug_inn: string | null
  design_key: string | null
  is_nti: boolean | null
  pathway_keys: string[]
  prohibited: DdiRowData[]
  restricted: DdiRowData[]
  permitted: DdiRowData[]
  max_washout_days: number | null
  screening_history_window_days: number | null
  /** Number of unresolved-pathway notes (engine fell back to a pan-pathway prohibition). */
  unresolved_count: number
  summary: string | null
}

/* design_decisions */

export interface DesignDecisionData {
  key: string
  label: string
  value: string
  rationale: string
  evidence: string
  options: string[]
}

export interface DesignDecisionsCardData {
  drug_inn: string | null
  study_type: StudyType | null
  decisions: DesignDecisionData[]
  /** True once the user clicked "Confirm design". */
  confirmed: boolean
}

/* indication */

export interface IndicationCandidateData {
  indication: string
  on_label: boolean
  label_evidence: string | null
  therapeutic_area: string | null
  setting_note: string | null
  monotherapy: string | null
  pk_variability: string | null
  observed_cv_pct: number | null
  recruitment: string | null
  rank_reason: string | null
}

export interface IndicationCardData {
  /** False when the study is not a patient biosimilar study (`reason` says why). */
  applicable: boolean
  reason: string | null
  drug_inn: string | null
  drug_class: string | null
  label_verified: boolean | null
  recommended: IndicationCandidateData | null
  ranked: IndicationCandidateData[]
  user_indication: string | null
  user_indication_warnings: string[]
  /** The indication currently chosen in the spec (null = the engine chooses). */
  selected_indication: string | null
  summary: string | null
}

/* guideline */

export interface GuidelineOptionData {
  id: string
  display_name: string
  regulator: string
  region: string
  citation: string
  is_current: boolean
  submission_pathway: string
}

export interface GuidelineCardData {
  selected: string
  default: string
  options: GuidelineOptionData[]
}

/* protocol_summary / generation_progress / protocol_ready */

export interface ProtocolSummaryCardData {
  rows: SummaryRow[]
  ready: boolean
  /** Labels of what is still required (empty when ready). */
  missing: string[]
  /** A server refusal that blocks Generate even though the spec is complete. */
  blocking_issue: string | null
  notes: string[]
}

export interface GenerationProgressCardData {
  protocol_id: string
  drug_inn: string | null
  /** "generating" | "complete" | "failed" (Protocol.generation_status). */
  status: string
  error: string | null
}

export interface ProtocolReadyCardData {
  protocol_id: string
  drug_inn: string | null
  study_type: string | null
  guideline_version: string | null
  section_count: number
  version: number
}

/* budget_preview / budget_summary / budget_ready */

export interface BudgetDefaultsData {
  n_enrolled: number | null
  n_sites: number | null
  n_countries: number | null
  project_duration_months: number | null
  startup_months: number | null
  recruitment_months: number | null
  treatment_months: number | null
  followup_months: number | null
  closeout_months: number | null
  n_expected_saes: number | null
  geography_profile: string | null
}

export interface BudgetPreviewCardData {
  drug_inn: string
  modality: Modality
  allowed_study_types: string[]
  study_type: string
  subject_profile: string
  subject_profile_source: string
  subject_profile_basis: string
  therapeutic_area: string
  profile_key: string | null
  defaults: BudgetDefaultsData
  estimated_grand_total: number
  currency: string
  category_count: number
  disclaimer: string
}

export interface ProtocolOption {
  id: string
  drug_inn: string
  study_type: string
  status: string
  version: number
  created_at: string | null
}

export interface BudgetSummaryCardData {
  rows: SummaryRow[]
  ready: boolean
  missing: string[]
  blocking_issue: string | null
  /** The protocol currently linked in the spec, if any. */
  linked_protocol: ProtocolOption | null
  /** Org protocols that can be linked (empty unless the assistant listed them). */
  protocol_options: ProtocolOption[]
  /** The engine's estimate for the current drug (null until a preview ran). */
  estimated_grand_total: number | null
  currency: string
  notes: string[]
}

export interface BudgetReadyCardData {
  budget_id: string
  name: string
  drug_inn: string | null
  study_type: string | null
  currency: string
  grand_total: number | null
  category_count: number
  n_enrolled: number | null
  protocol_id: string | null
}

/* protocol_overview / edit_impact / edit_progress / edit_diff */

export interface SectionStat {
  name: string
  label: string
  words: number
}

export interface ProtocolOverviewCardData {
  protocol_id: string
  drug_inn: string | null
  status: string
  version: number
  study_type: string
  guideline_version: string
  generation_status: string
  /** True when chat edits are allowed (status == draft and generation complete). */
  editable: boolean
  blocked_reason: string | null
  sections: SectionStat[]
  /** Engine-locked facts (N, design, CV and its source, decisions, DDI rows...). */
  locked_facts: FactRow[]
}

export interface EditChange {
  field: string
  label: string
  before: string | null
  after: string | null
}

export interface EditEstimate {
  label: string
  rows: FactRow[]
  note: string
}

export interface EditImpactCardData {
  /** Pre-minted id of the proposal the confirm_edit action will create. */
  proposal_id: string
  kind: ProposalKind
  base_version: number
  editable: boolean
  blocked_reason: string | null
  /** Engine edits: the inputs that change. */
  changes: EditChange[]
  /** Prose edits: the section names to rewrite. */
  sections: string[]
  instruction: string | null
  estimate: EditEstimate | null
  warnings: string[]
  rerun_note: string
}

export interface EditProgressCardData {
  proposal_id: string
  kind: ProposalKind
  status: ProposalStatus
  error: string | null
}

export interface EditDiffCardData {
  proposal_id: string
  kind: ProposalKind
  status: ProposalStatus
  base_version: number
  /** True when the protocol changed after the proposal was made (Apply is blocked). */
  stale: boolean
  sections: SectionDiffItem[]
  qc: Record<string, unknown> | null
  facts_before: FactRow[]
  facts_after: FactRow[]
  applied_version: number | null
}

/* notice */

export interface NoticeCardData {
  level: NoticeLevel
  text: string
  code: string | null
}

/** CardType -> the shape of its `data` (the TypeScript twin of CARD_DATA_MODELS). */
export interface CardDataMap {
  drug_matches: DrugMatchesCardData
  drug_add_offer: DrugAddOfferCardData
  study_type: StudyTypeCardData
  population: PopulationCardData
  cv_override_warning: CvOverrideWarningCardData
  interaction_screening: InteractionScreeningCardData
  design_decisions: DesignDecisionsCardData
  indication: IndicationCardData
  guideline: GuidelineCardData
  protocol_summary: ProtocolSummaryCardData
  generation_progress: GenerationProgressCardData
  protocol_ready: ProtocolReadyCardData
  budget_preview: BudgetPreviewCardData
  budget_summary: BudgetSummaryCardData
  budget_ready: BudgetReadyCardData
  protocol_overview: ProtocolOverviewCardData
  edit_impact: EditImpactCardData
  edit_progress: EditProgressCardData
  edit_diff: EditDiffCardData
  notice: NoticeCardData
}

/** A Card whose `data` is typed for card type `T`. */
export type TypedCard<T extends CardType = CardType> = Omit<Card, 'type' | 'data'> & {
  type: T
  data: CardDataMap[T]
}
