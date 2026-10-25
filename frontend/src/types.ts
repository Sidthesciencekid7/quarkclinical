export type DrugModality = 'biologic' | 'small_molecule' | 'unknown'

export interface Drug {
  id: string
  inn: string
  brand_names: string[]
  rxnorm_id: string | null
  updated_at: string
  modality?: DrugModality
}

export type UserRole = 'generator' | 'reviewer' | 'approver'

export const ROLE_DISPLAY_NAMES: Record<UserRole, string> = {
  generator: 'Author',
  reviewer: 'Reviewer',
  approver: 'Approver',
}

export interface AppUser {
  id: string
  org_id: string
  email: string
  role: UserRole
}

// ── Organization / license / team ────────────────────────────────────────
// No Stripe integration — enterprise deals are sales-assisted. A self-serve
// org's license starts "pending"; the operator manually activates it via
// POST /api/orgs/{id}/activate-license once a deal is confirmed.

export interface OrgLicense {
  status: 'active' | 'suspended' | 'pending'
  seat_count: number
  seats_used: number
  period_start: string
  period_end: string
}

export interface Org {
  id: string
  name: string
  slug: string
  is_owner: boolean
  license: OrgLicense | null
  /** Only present when the caller is the org owner. */
  join_token: string | null
}

export interface OrgMember {
  id: string
  email: string
  role: UserRole
  is_org_owner: boolean
}

// ── Leads ("Contact sales") ──────────────────────────────────────────────

export interface LeadCreatePayload {
  company_name: string
  contact_name: string
  contact_email: string
  team_size_estimate?: string
  message?: string
}

export type LeadStatus = 'new' | 'contacted' | 'won' | 'lost'

export interface Lead {
  id: string
  company_name: string
  contact_name: string
  contact_email: string
  team_size_estimate: string | null
  message: string | null
  status: LeadStatus
  created_at: string
}

// ── Admin dashboard ───────────────────────────────────────────────────────

export interface AdminOrgRow {
  id: string
  name: string
  slug: string
  owner_email: string | null
  license_status: string | null
  seat_count: number | null
  seats_used: number
  /** Days left in the current license period — only set while active. */
  days_remaining: number | null
  created_at: string
}

export type StudyType = 'BE' | 'biosimilar_pk' | 'food_effect'
export type SubjectType = 'healthy' | 'patient'
export type SexType = 'male' | 'female' | 'both'

export interface PopulationConfig {
  subject_type: SubjectType
  age_min: number
  age_max: number
  sex: SexType
  weight_min: number
  weight_max: number
  exclusion_presets: string[]
  /**
   * Extra PK blood-sampling timepoints to add to the auto-computed schedule,
   * as free text (e.g. "36, 48, 72" or "36h, 48h"), in HOURS post-dose. The
   * backend parses out any numbers it finds and ignores the rest rather than
   * rejecting the whole field — see services/sampling_schedule.py's
   * additional_timepoints_hours parameter. Left blank, only the auto-computed
   * schedule is used.
   */
  additional_pk_timepoints_hours?: string
  /**
   * Extra immunogenicity (ADA) sampling timepoints to add to the auto-computed
   * schedule, as free text, in MONTHS post-first-dose (matching the
   * comparative-safety period's own units) — e.g. "9, 15". See
   * services/comparative_safety.py's additional_months parameter. Only
   * meaningful for a multiple-dose biosimilar patient study, where an
   * immunogenicity schedule is computed at all.
   */
  additional_immunogenicity_timepoints_months?: string
  // Optional study-design parameters that drive the sample-size engine and the
  // generated protocol. Omitted keys fall back to literature / sensible defaults.
  /**
   * Within-subject CV %, OPTIONAL and normally left blank.
   *
   * Blank (the default and the safer path) = the powering CV is sourced
   * automatically from the literature — the highest within-subject CV reported
   * for Cmax or AUC across healthy-volunteer studies, with the supporting
   * trials tabulated in the SAP (routers/protocols.py::_select_cv_for_powering).
   *
   * A value here is an INVESTIGATOR-SPECIFIED OVERRIDE: it replaces the
   * literature-derived figure, and the protocol must declare it as such rather
   * than implying the SAP's supporting-studies table backs it.
   */
  cv_intra?: number
  reference_product?: string // named Reference Listed Drug
  dose_strength?: string     // e.g. "400 mg tablet"
  is_nti?: boolean           // narrow-therapeutic-index → tightened limits
  /**
   * Optional user-specified study indication for a patient biosimilar PK study
   * (a disease/setting name, max USER_INDICATION_MAX_CHARS). Blank = the engine
   * picks. The server re-sanitises it and only honours it for patient
   * biosimilar_pk studies.
   */
  user_indication?: string
}

export const defaultPopulationConfig: PopulationConfig = {
  subject_type: 'healthy',
  age_min: 18,
  age_max: 55,
  sex: 'both',
  weight_min: 50,
  weight_max: 100,
  // Only drug-AGNOSTIC criteria are pre-selected. Pathway-specific exclusions
  // (CYP/UGT/transporter perpetrators, grapefruit, tobacco-as-CYP1A2-inducer)
  // are supplied per drug by the backend DDI screening engine — a hardcoded
  // "CYP3A4 inhibitors or inducers" default used to be applied to every drug
  // regardless of whether it is a CYP3A4 substrate at all.
  exclusion_presets: [
    'Tobacco or nicotine use within 6 months',
  ],
}

export interface ProtocolCreatePayload {
  drug_id: string
  study_type: StudyType
  population_config: PopulationConfig
  guideline_version: string
  design_decisions?: DesignDecision[]
}

/** A selectable regulatory guideline (GET /api/guidelines). */
export interface AppGuideline {
  id: string
  display_name: string
  regulator: string
  region: string
  citation: string
  is_current: boolean
  /** Submission pathway for a small-molecule (BE / food-effect) study, e.g. the
   *  FDA ANDA 505(j) pathway. '' when the body grants no marketing
   *  authorisation (ICH). */
  submission_pathway?: string
  /** Submission pathway for a biosimilar PK-similarity study, e.g. FDA 351(k)
   *  (BPCIA). Different statute from the generic pathway above. */
  biosimilar_pathway?: string
  /** Governing citation for a biosimilar study; overrides `citation`, which is
   *  written in generic-drug terms. '' when the profile has no override. */
  biosimilar_citation?: string
  /** Selector label for a biosimilar study; overrides `display_name`, which is
   *  written in generic-drug terms (e.g. "Bioequivalence (ANDA, 2014)"). ''
   *  when the generic label is already accurate for a biosimilar. */
  biosimilar_display_name?: string
}

export interface GuidelinesResponse {
  default: string
  guidelines: AppGuideline[]
}

export interface GeneratedProtocol {
  id: string
  drug_id: string
  org_id: string
  study_type: StudyType
  population_config: Record<string, unknown>
  sections: Record<string, string>
  version: number
  status: string
  guideline_version: string
  // Async-generation lifecycle: 'generating' while the background pipeline runs,
  // 'complete' once sections are filled, 'failed' if it couldn't. createProtocol
  // returns 'generating' immediately; the UI polls getProtocol until it flips.
  // Absent/'complete' for every already-generated protocol (server default).
  generation_status?: 'generating' | 'complete' | 'failed'
  generation_error?: string | null
  /** Present on newer servers (ProtocolRead): the drug's INN and brand names. */
  drug_inn?: string
  drug_brand_names?: string[]
  created_by: string
  created_at: string
}

/** One of the five study-design decisions from the "Review design" step. */
export interface DesignDecision {
  key: string
  label: string
  value: string
  rationale: string
  evidence: string
  options: string[]
}

export const USER_INDICATION_MAX_CHARS = 200
export const POP_PATIENTS_MULTI = 'Patients · multi-dose'
export const DOSE_SINGLE = 'Therapeutic (label) dose — single dose'
export const DOSE_MULTIPLE_SS = 'Therapeutic (label) dose — multiple doses to steady state'

export type TumorSetting = 'adjuvant' | 'low_burden' | 'high_burden' | 'unknown' | 'not_applicable'
export type TherapeuticArea = 'oncology' | 'immunology' | 'other'

/** One candidate study indication from the patient-population engine
 *  (backend services/patient_population.py). */
export interface IndicationCandidate {
  indication: string
  on_label: boolean
  label_evidence: string
  therapeutic_area?: TherapeuticArea
  tumor_setting?: TumorSetting
  setting_note?: string
  curation_source?: string
  monotherapy: string
  monotherapy_note: string
  pk_variability: string
  pk_variability_note: string
  observed_cv_pct: number | null
  recruitment: string
  recruitment_note: string
  naive_feasible: boolean
  naive_note: string
  rank_reason: string
  user_specified?: boolean
}

/** POST /api/protocols/indication-preview response. */
export interface PatientPopulationPreview {
  applicable: boolean
  reason?: string
  drug_inn?: string
  drug_class?: string
  label_verified?: boolean
  recommended?: IndicationCandidate | null
  engine_recommended?: IndicationCandidate | null
  ranked?: IndicationCandidate[]
  rejected?: IndicationCandidate[]
  user_specified?: boolean
  user_indication?: string | null
  user_indication_matched_candidate?: string | null
  user_indication_warnings?: string[]
  no_on_label_candidate?: boolean
  selection_rules?: string[]
  assumptions?: string[]
  summary?: string
}

export interface DesignPreviewResponse {
  drug_inn: string
  study_type: StudyType
  decisions: DesignDecision[]
  /** Present only if the backend attaches the DDI screening to this preview
   *  instead of exposing it on its own route (see api/client.ts::previewDdi). */
  ddi?: DdiScreeningResult
}

// ── Drug-drug interaction screening (backend services/ddi_screening.py) ────
// The engine reads the drug's real DailyMed label, detects which metabolic and
// transporter pathways actually apply, and returns the comedication rows that
// belong in the exclusion criteria. This is what replaces the old static
// "CYP3A4 inhibitors or inducers within 14 days" preset, which was offered for
// every drug regardless of whether it is a CYP3A4 substrate.

export type DdiStatus = 'prohibited' | 'restricted' | 'permitted'

export interface DdiEvidence {
  /** Which label section the cue was found in. */
  source?: string | null
  /** The regex cue that matched. */
  cue?: string | null
  /** The matched label text. */
  quote?: string | null
}

/** One comedication row. Flat by design — it renders straight into a table. */
export interface DdiRow {
  key: string
  label: string
  status: DdiStatus | string
  pathway?: string | null
  examples?: string[]
  washout_days?: number | null
  washout_basis?: string | null
  extended_washout?: boolean
  history_exclusion?: boolean
  rationale?: string | null
  conditions?: string | null
  escalated_by_nti?: boolean
  /** Set when the row rests on a stated convention rather than pharmacology. */
  assumption?: string | null
  evidence?: DdiEvidence | null
  /** Optional server-rendered protocol sentence. Used verbatim when present so
   *  the wording the protocol carries is the wording the engine wrote. */
  criterion?: string | null
}

export interface DdiScreeningResult {
  drug_inn?: string
  design_key?: string | null
  is_nti?: boolean | null
  /** Detected pathway records. The UI only relies on `pathway_keys`. */
  pathways?: unknown[]
  /** e.g. ["cyp3a4", "pgp"] — empty when the label identified nothing. */
  pathway_keys?: string[]
  prohibited?: DdiRow[]
  restricted?: DdiRow[]
  permitted?: DdiRow[]
  max_washout_days?: number | null
  screening_history_window_days?: number | null
  /** Agents excluded on history because they cannot be washed out. */
  history_exclusion_agents?: string[]
  study_drug_t_half_hours?: number | null
  post_dose_restriction_hours?: number | null
  /** Non-empty when no pathway could be resolved from the label — the engine
   *  then falls back to one conservative pan-pathway prohibition. Shape is
   *  server-owned; the UI only counts these, it never renders their contents. */
  unresolved?: unknown[]
  assumptions?: unknown[]
  summary?: string
}

/** A row in the "My Reports" history list (GET /api/protocols). */
export interface ProtocolSummary {
  id: string
  drug_id: string
  drug_inn: string
  drug_brand_names: string[]
  study_type: StudyType
  guideline_version: string
  guideline_outdated: boolean
  version: number
  status: string
  section_count: number
  created_at: string
}

export interface SectionUpdateResult {
  protocol_id?: string
  section_name: string
  content: string
  version: number
}

/** A persisted section review note. Stored server-side (`comments` table) and
 *  shared across the org; the API's snake_case CommentRead is mapped to this
 *  camelCase shape in api/client.ts. */
export interface Comment {
  id: string
  sectionName: string
  text: string
  author: string
  createdAt: string
  resolved: boolean
}

// ── Protocol QC (deterministic self-review) ──────────────────────────────
export type QCStatus = 'pass' | 'warning' | 'fail' | 'assumption' | 'not_applicable'

export interface QCFinding {
  id: string
  category: string
  title: string
  status: QCStatus
  detail: string
  section: string | null
}

export interface QCReport {
  submission_ready: boolean
  counts: Record<QCStatus, number>
  total_checks: number
  findings: QCFinding[]
}

// ── Guideline coverage matrix ────────────────────────────────────────────
export type CovStatus = 'covered' | 'partial' | 'missing' | 'not_applicable'

export interface CoverageItem {
  id: string
  requirement: string
  guidance: string
  sections: string[]
  status: CovStatus
  detail: string
}

export interface CoverageReport {
  coverage_pct: number
  counts: Record<CovStatus, number>
  total: number
  items: CoverageItem[]
}

// Canonical order matching the backend (protocols.py SECTION_ORDER). Every
// section is generated for every study type — immunogenicity-specific content
// is conditioned internally (within study_assessments / statistical_analysis_plan)
// based on study_type rather than being its own top-level section.
export const SECTION_ORDER = [
  'title',
  'synopsis',
  'introduction',
  'study_rationale',
  'objectives',
  'endpoints',
  'study_design',
  'population',
  'treatment',
  'study_assessments',
  'safety_monitoring',
  'data_handling',
  'study_monitoring',
  'statistical_analysis_plan',
  'ethics',
  'administrative',
  'references',
] as const

export type SectionName = typeof SECTION_ORDER[number]

export const SECTION_DISPLAY_NAMES: Record<SectionName, string> = {
  title: 'Title Page',
  synopsis: 'Synopsis',
  introduction: 'Introduction',
  study_rationale: 'Study Rationale & Risk/Benefit',
  objectives: 'Study Objectives',
  endpoints: 'Study Endpoints',
  study_design: 'Study Design',
  population: 'Population',
  treatment: 'Treatment',
  study_assessments: 'Study Assessments',
  safety_monitoring: 'Safety Monitoring',
  data_handling: 'Data Handling & Record Keeping',
  study_monitoring: 'Study Monitoring, Auditing & Inspecting',
  statistical_analysis_plan: 'Statistical Methods & Data Analysis',
  ethics: 'Ethical & Regulatory Standards',
  administrative: 'Administrative Procedures',
  references: 'References',
}

/** Every section applies to every study type now (see SECTION_ORDER comment above). */
export function visibleSections(_studyType: StudyType): SectionName[] {
  return SECTION_ORDER as unknown as SectionName[]
}

// ── Risk / feasibility scoring ────────────────────────────────────────────
// GET /api/protocols/{id}/risk — deterministic operational-risk report.
// overall_score is 0-100 where HIGHER means RISKIER.

export type RiskLevel = 'low' | 'moderate' | 'high'

export interface RiskFactor {
  label: string
  severity: RiskLevel
  detail: string
}

export interface RiskScore {
  overall_score: number
  risk_level: RiskLevel
  factors: RiskFactor[]
}

// ── Clinical Study Report (CSR) — post-study results + ICH E3 narrative ────
// A StudyReport is the post-study bookend to a Protocol: the company's real
// results are analysed DETERMINISTICALLY into `results` (never AI-touched) and
// the AI drafts the E3 narrative into `sections` around those locked numbers.

export type CsrInputKind = 'nca' | 'params'
export type CsrVerdict = 'bioequivalent' | 'not_bioequivalent' | 'indeterminate'

/** One PK metric's bioequivalence result (percentages are % of Reference). */
export interface BeMetricResult {
  metric: string
  n?: number
  point_estimate_pct?: number
  ci_lower_pct?: number | null
  ci_upper_pct?: number | null
  cv_intra_pct?: number
  df?: number
  within_be_limits?: boolean
  lower_limit_pct?: number
  upper_limit_pct?: number
  method?: string
  // Replicate/RSABE-only:
  scaled?: boolean
  cv_wr_pct?: number
  criterion_upper_bound?: number | null
  // Present instead of stats when a metric could not be analysed:
  error?: string
}

export interface CsrDisposition {
  n_subjects?: number
  n_rows?: number
  treatments?: string[]
  periods?: number[]
  sequences?: string[]
  n_blq?: number
  lloq?: number | null
}

/** The locked, deterministically-computed results object. */
export interface CsrResults {
  input_kind?: CsrInputKind
  design_key?: string
  is_nti?: boolean
  co_primary?: string[]
  verdict?: CsrVerdict
  disposition?: CsrDisposition
  be?: Record<string, BeMetricResult>
  nca?: Record<string, Record<string, number | string | null>>
}

export interface StudyReport {
  id: string
  protocol_id: string
  org_id: string
  input_kind: CsrInputKind
  dataset_checksum?: string | null
  dataset_summary?: Record<string, unknown>
  results?: CsrResults
  sections?: Record<string, string>
  version: number
  generation_status?: 'generating' | 'complete' | 'failed'
  generation_error?: string | null
  created_by: string
  created_at: string
}

export interface StudyReportSummary {
  id: string
  protocol_id: string
  input_kind: CsrInputKind
  version: number
  generation_status: string
  has_results: boolean
  verdict?: CsrVerdict | null
  created_at: string
}

export interface CsrQCFinding {
  id: string
  category: string
  title: string
  status: string
  detail: string
  section: string | null
}

export interface CsrQCReport {
  submission_ready: boolean
  counts: Record<string, number>
  total_checks: number
  findings: CsrQCFinding[]
}

// ICH E3 CSR section order + labels (mirrors backend services/csr_sections.py).
export const CSR_SECTION_ORDER = [
  'title', 'synopsis', 'introduction', 'study_objectives', 'investigational_plan',
  'study_population', 'pharmacokinetic_results', 'bioequivalence_assessment',
  'safety_results', 'discussion_conclusions', 'references',
] as const

export const CSR_SECTION_TITLES: Record<string, string> = {
  title: 'Title',
  synopsis: 'Synopsis',
  introduction: 'Introduction',
  study_objectives: 'Study Objectives',
  investigational_plan: 'Investigational Plan',
  study_population: 'Study Population and Subject Disposition',
  pharmacokinetic_results: 'Pharmacokinetic Results',
  bioequivalence_assessment: 'Bioequivalence Assessment',
  safety_results: 'Safety Evaluation',
  discussion_conclusions: 'Discussion and Overall Conclusions',
  references: 'References',
}

// ── Budgeting Tool ────────────────────────────────────────────────────────
// Wire shapes for /api/budgets. Money is a JSON number or null throughout —
// NEVER a string, and never recomputed on the client: the server owns every
// subtotal, the discount, and the grand total (see BudgetRollup).

export type BudgetStatus = 'draft' | 'final' | 'archived'

/** Which rollup bucket a category feeds. Only `direct` is discountable.
 *  `investigator_grants` and `software` are patient-study buckets — they are
 *  always 0.0 on a healthy-volunteer budget. */
export type BudgetBucket =
  | 'direct'
  | 'stipend'
  | 'variable'
  | 'pass_through'
  | 'investigator_grants'
  | 'software'

export type BudgetDiscountType = 'none' | 'percent' | 'flat'

/** 'healthy' (the only kind that existed before this) or 'patient'. Drives a
 *  whole second rate card, extra budget-level drivers (§ below) and two extra
 *  rollup buckets. Server-resolved from the drug KB when omitted at create. */
export type BudgetSubjectProfile = 'healthy' | 'patient'

/** How aggressively a unit cost is discounted from a US baseline for
 *  non-US-heavy enrollment. Only meaningful for patient budgets. */
export type BudgetGeographyProfile =
  | 'us_only'
  | 'eu_west'
  | 'eu_east'
  | 'latam'
  | 'apac'
  | 'china'
  | 'india'
  | 'global_emerging_mixed'
  | 'global_developed_mixed'

/** How solid a `researched` line's unit cost actually is. Only meaningful
 *  when `source === 'researched'` — null otherwise. */
export type BudgetProvenanceTier = 'published_rate' | 'derived' | 'bid_calibrated' | 'estimate'

/** Only `costed` rows carry money into a subtotal. The other five are the real
 *  CRO vocabulary for "this row exists but has no total" and render as a status
 *  label where the money would be. */
export type BudgetLineStatus =
  | 'costed'
  | 'out_of_scope'
  | 'not_applicable'
  | 'not_planned'
  | 'outsourced'
  | 'actuals_only'

/** `protocol_derived` = the system found this cost driver in the linked
 *  protocol's computed schedule. `rate_card` = both quantity and unit cost
 *  are calibrated approximations from the server's rate card. `researched` =
 *  the unit cost is a real, cited, externally-published figure (or a
 *  documented triangulation of such figures) — see `derivation.sources` for
 *  the citation, tier and caveat, referenced by the line's `source_id`. Like
 *  every other value here this is a provenance badge that SURVIVES a user
 *  edit; "edited-ness" is answered separately by comparing `unit_cost` to
 *  `derivation.auto_priced[derivation_key]`. Never render `rate_card`
 *  identically to `manual` (typed by a person) or to `protocol_derived`
 *  (quantity came from a real linked protocol). */
export type BudgetLineSource = 'manual' | 'protocol_derived' | 'rate_card' | 'researched'

/** Namespaced, server-defined keys (e.g. "dm.edc_license",
 *  "stipend.completed") — never a closed set, never matched against one. */
export type BudgetDerivationKey = string

export interface BudgetLineItem {
  id: string
  category_id: string
  label: string
  quantity: number | null
  unit_cost: number | null
  unit_description: string | null
  total: number | null
  status: BudgetLineStatus
  source: BudgetLineSource
  derivation_key: BudgetDerivationKey | null
  note: string | null
  sort_order: number
  created_at: string
  updated_at: string
}

/** A real, citable external figure backing one or more `researched` lines.
 *  Stored once per source in `budget.derivation.sources`, keyed by id, and
 *  referenced from each line's derivation record via `source_id`. */
export interface BudgetSourceRecord {
  tier: BudgetProvenanceTier
  /** The figure AS PUBLISHED, e.g. "USD 675 per site per year". */
  figure: string
  citation: string
  url: string | null
  accessed: string
  /** What the figure is NOT, in the author's own words — rendered in amber. */
  caveat: string | null
}

export interface BudgetCategory {
  id: string
  budget_id: string
  name: string
  bucket: BudgetBucket
  sort_order: number
  note: string | null
  /** Already sorted by sort_order by the server. */
  line_items: BudgetLineItem[]
  /** Sum of `costed` line totals. Server-computed — see BudgetRollup for why
   *  the UI reads `rollup.category_subtotals[id]` instead of this field after
   *  a write. */
  subtotal: number
}

export interface BudgetRollup {
  direct_subtotal: number
  discount_type: BudgetDiscountType
  discount_value: number
  discount_amount: number
  total_direct_costs: number
  subject_stipend_total: number
  variable_total: number
  pass_through_total: number
  /** Patient-study bucket. 0.0 on every healthy-volunteer budget. */
  investigator_grants_total: number
  /** Patient-study bucket. 0.0 on every healthy-volunteer budget. */
  software_total: number
  grand_total: number
  /** category id -> subtotal. The single freshest source of every per-category
   *  number after any line-item write. */
  category_subtotals: Record<string, number>
}

/** One cost driver the protocol/rate-card produced, as the derivation record
 *  reports it. `unit_cost`/`total`/`source`/`category`/`basis` are populated
 *  for the auto-population engine's rows (both protocol_derived and
 *  rate_card); a legacy protocol-only derivation may omit them. */
export interface BudgetDerivationLine {
  derivation_key: BudgetDerivationKey
  label: string
  quantity: number
  unit_description: string | null
  unit_cost?: number | null
  total?: number | null
  source?: BudgetLineSource
  category?: string
  basis?: string
  /** Provenance tier of a `researched` line's unit cost. Null for anything
   *  else — never render a tier badge when this is null. */
  provenance_tier?: BudgetProvenanceTier | null
  /** Key into `derivation.sources`. Null when the tier is `estimate` (no
   *  citation exists) or the line isn't `researched` at all. */
  source_id?: string | null
  /** The published rate BEFORE the geography factor was applied. Null when
   *  the line isn't geography-scaled — compare against `unit_cost` to decide
   *  whether to render the "(26,000 × 0.45 geo)" suffix at all. */
  unit_cost_published?: number | null
  /** The geography factor actually applied to this line, if any. */
  geography_factor?: number | null
}

/** One auto-populated field's provenance: what value was used, where it came
 *  from, and a one-line human explanation (`basis`) safe to render verbatim.
 *  `inferred_from_safety_profile` = the subject_profile was resolved from the
 *  drug's safety KB (e.g. "not NHV-safe"). `phase_scaled` = derived by
 *  scaling a per-month or per-visit rate across the study's phase durations.
 *  `kb_default` = a knowledge-base default for a drug not otherwise in the
 *  safety KB. */
export interface BudgetDerivationAssumption {
  value: number | string | boolean | null
  source:
    | 'user'
    | 'protocol'
    | 'default'
    | 'computed'
    | 'inferred_from_modality'
    | 'inferred_from_safety_profile'
    | 'phase_scaled'
    | 'kb_default'
  basis: string
}

/** Provenance of the budget's figures — rendered as "how we got these
 *  numbers". `warnings` are shown VERBATIM, never paraphrased. `protocol_id`
 *  is null for a drug-name-only budget (no protocol involved at all). */
export interface BudgetDerivation {
  protocol_id: string | null
  derived_at: string
  n_enrolled: number | null
  n_pk_timepoints: number | null
  n_ada_timepoints: number | null
  n_visit_days: number | null
  design_key: string | null
  periods: number | null
  lines: BudgetDerivationLine[]
  warnings: string[]
  /** Always false now — do not branch on it. */
  skipped: boolean
  engine?: 'budget_auto_population'
  rate_card_version?: string
  mode?: 'create' | 'reconcile'
  drug_inn?: string | null
  modality?: 'biologic' | 'small_molecule' | 'unknown'
  study_type?: string | null
  complexity?: 'simple' | 'complex'
  currency?: string
  /** True only when a real protocol was linked — a drug-name-only budget has
   *  a full derivation too, just with this false and protocol_id null. */
  protocol_linked?: boolean
  assumptions?: Record<string, BudgetDerivationAssumption>
  conduct?: {
    target_per_enrolled: number
    pk_draw_rate: number
    ada_draw_rate: number
    visit_day_rate: number
    itemized_per_enrolled: number
    residual_per_enrolled: number
    screen_failure_ratio: number
    alternate_ratio: number
  }
  line_keys?: string[]
  /** derivation_key -> the price the engine assigned. Compare a line's live
   *  `unit_cost` against this (not `source`) to detect a user's correction —
   *  PATCH refuses to change source/derivation_key, so a corrected rate_card
   *  row keeps its "Estimated" badge forever. */
  auto_priced?: Record<string, number | null>
  user_removed_keys?: string[]
  estimated_grand_total?: number
  /** Rendered verbatim as the editor's top-of-page disclosure line. */
  disclaimer?: string
  discount_note?: string

  /** Real-drug trial-history grounding (services/budget_trial_precedent.py):
   *  which of this DRUG's own ClinicalTrials.gov trials, if any, informed
   *  n_enrolled/n_sites/n_countries instead of the generic per-study-type
   *  default. Always present — `found: false` is an honest "no usable trial
   *  history for this drug", not an absent key. */
  trial_precedent?: {
    found: boolean
    trials: {
      nct_id: string
      title: string
      status: string
      phase: string
      enrollment_count: number | null
      enrollment_type: string
      n_countries: number
      n_locations: number
      duration_months: number | null
    }[]
    median_n_enrolled: number | null
    median_n_countries: number | null
    median_n_sites: number | null
    median_duration_months: number | null
  }

  // ── Patient-study additions (absent/null on a healthy budget) ──────────
  subject_profile?: BudgetSubjectProfile
  therapeutic_area?: string | null
  indication?: string | null
  /** Which patient rate-card profile priced this budget, e.g.
   *  "biosimilar_pk_oncology". */
  profile_key?: string | null
  geography_profile?: BudgetGeographyProfile | null
  geography_factor?: number | null

  /** Phase-duration math behind every per-month-priced line. */
  phases?: {
    startup_months: number
    recruitment_months: number
    treatment_months: number
    followup_months: number
    closeout_months: number
    total_months: number
    active_months: number
    site_months_startup: number
    site_months_active: number
    site_months_closeout: number
    site_years: number
    ramp_factors: { startup: number; active: number; closeout: number }
  }

  /** One entry per citation referenced by a `researched` line, keyed by the
   *  same `source_id` the line carries. Rendered verbatim — never paraphrase
   *  a citation or caveat. */
  sources?: Record<string, BudgetSourceRecord>

  provenance_summary?: {
    researched_lines: number
    researched_value: number
    derived_lines: number
    derived_value: number
    bid_calibrated_lines: number
    bid_calibrated_value: number
    estimate_lines: number
    estimate_value: number
    researched_share_of_grand_total: number
  }

  /** Patient budgets only: the honesty block comparing this estimate against
   *  real CRO bids for a comparable reference study. Disclosure, not an
   *  error — render with an amber border, never red. */
  bid_reconciliation?: {
    reference_study: string
    bids: { cro: string; grand_total: number | null; note?: string }[]
    this_estimate: number
    delta_vs_highest_bid_pct: number
    explained_by: string[]
    disclaimer: string
  }

  /** Deterministic sanity checks against published cost-share benchmarks
   *  (e.g. "site monitoring should be 9-14% of grand total"). A `pass: false`
   *  entry also appears, verbatim, in `warnings`. */
  cross_checks?: {
    id: string
    value: number
    band: [number, number]
    pass: boolean
    source_id?: string | null
    message?: string
  }[]
}

export interface Budget {
  id: string
  org_id: string
  protocol_id: string | null
  name: string
  sponsor_name: string | null
  cro_name: string | null
  currency: string
  status: BudgetStatus
  // Auto-estimated by the server on create; every one of these is editable.
  drug_inn: string | null
  study_type: StudyType | null
  n_enrolled: number | null
  screen_failure_rate_pct: number | null
  n_screen_failures: number | null
  n_alternates: number | null
  n_screened: number | null
  n_sites: number | null
  n_dosing_groups: number | null
  project_duration_months: number | null
  discount_type: BudgetDiscountType
  discount_value: number
  notes: string | null
  derivation: BudgetDerivation | null
  categories: BudgetCategory[]
  rollup: BudgetRollup
  created_at: string
  updated_at: string

  // ── Patient-study drivers (§7.2/7.5) — null/omitted on a healthy budget ──
  /** Server-resolved unless explicitly set at create/update. */
  subject_profile: BudgetSubjectProfile
  therapeutic_area: string | null
  indication: string | null
  n_countries: number | null
  n_sites_selected: number | null
  dropout_rate_pct: number | null
  startup_months: number | null
  recruitment_months: number | null
  treatment_months: number | null
  followup_months: number | null
  closeout_months: number | null
  n_expected_saes: number | null
  n_expected_susars: number | null
  n_monitoring_visits_onsite: number | null
  n_monitoring_visits_remote: number | null
  n_pharmacy_visits: number | null
  n_visits_per_patient: number | null
  n_protocol_amendments: number | null
  geography_profile: BudgetGeographyProfile | null
  geography_factor: number | null
  archival_retention_years: number | null
  bicr_required: boolean | null
  comparator_sourced_by_sponsor: boolean | null
  comparator_arm_fraction: number | null
  /** Server-computed: n_enrolled minus expected dropouts. Read-only. */
  n_completed: number | null
}

export interface BudgetSummary {
  id: string
  name: string
  protocol_id: string | null
  drug_inn: string | null
  study_type: StudyType | null
  status: BudgetStatus
  currency: string
  n_enrolled: number | null
  category_count: number
  line_item_count: number
  grand_total: number
  created_at: string
  updated_at: string
  subject_profile: BudgetSubjectProfile
  therapeutic_area: string | null
  n_sites: number | null
  n_countries: number | null
}

// ── Budget request payloads ──────────────────────────────────────────────
// IMPORTANT for every payload below: `undefined` means "omit the key" and
// `null` means "send an explicit null". `JSON.stringify` drops undefined keys,
// so one object literal expresses both. Never write `total: maybeNumber` when
// you mean "let the server compute it" — see BudgetLineItemCreatePayload.

/** Give it a drug_inn (or a name) and the server returns a complete,
 *  fully-priced budget. Any numeric field here overrides both the protocol
 *  and the rate-card default. */
export interface BudgetCreatePayload {
  /** Optional now — omit it and the server names the budget
   *  "<Drug> study budget". */
  name?: string
  /** THE primary input. A drug INN on its own produces a complete,
   *  fully-priced budget. */
  drug_inn?: string
  study_type?: StudyType
  protocol_id?: string | null
  sponsor_name?: string | null
  cro_name?: string | null
  currency?: string
  n_enrolled?: number | null
  n_sites?: number | null
  n_dosing_groups?: number | null
  project_duration_months?: number | null
  screen_failure_rate_pct?: number | null
  n_screen_failures?: number | null
  n_alternates?: number | null
  n_screened?: number | null
  notes?: string | null

  // ── Patient-study drivers — all optional; omitting every one preserves
  // today's exact healthy-volunteer behaviour. See §7.2 for the 422 rules
  // (dropout_rate_pct 0..100, geography_factor 0.05..2.0, the five *_months
  // and every n_* non-negative, comparator_arm_fraction 0..1).
  subject_profile?: BudgetSubjectProfile
  therapeutic_area?: 'oncology' | 'other' | 'unknown'
  indication?: string
  n_countries?: number
  n_sites_selected?: number
  dropout_rate_pct?: number
  startup_months?: number
  recruitment_months?: number
  treatment_months?: number
  followup_months?: number
  closeout_months?: number
  n_expected_saes?: number
  n_expected_susars?: number
  n_monitoring_visits_onsite?: number
  n_monitoring_visits_remote?: number
  n_pharmacy_visits?: number
  n_visits_per_patient?: number
  n_protocol_amendments?: number
  geography_profile?: BudgetGeographyProfile
  geography_factor?: number
  archival_retention_years?: number
  bicr_required?: boolean
  comparator_sourced_by_sponsor?: boolean
  comparator_arm_fraction?: number
}

/** Every key optional: omitted = unchanged, explicit null = cleared.
 *  `protocol_id` is deliberately absent — link/unlink goes through
 *  api.linkBudgetProtocol. A percent discount must be 0..100 (else 422), so
 *  validate before sending. Same new patient-study fields as
 *  BudgetCreatePayload, same `exclude_unset` semantics: omitted = untouched,
 *  explicit null = cleared (and re-derived on the next reprice). */
export interface BudgetUpdatePayload {
  name?: string
  sponsor_name?: string | null
  cro_name?: string | null
  currency?: string
  status?: BudgetStatus
  drug_inn?: string | null
  study_type?: StudyType | null
  n_enrolled?: number | null
  screen_failure_rate_pct?: number | null
  n_screen_failures?: number | null
  n_alternates?: number | null
  n_screened?: number | null
  n_sites?: number | null
  n_dosing_groups?: number | null
  project_duration_months?: number | null
  discount_type?: BudgetDiscountType
  discount_value?: number
  notes?: string | null

  subject_profile?: BudgetSubjectProfile | null
  therapeutic_area?: 'oncology' | 'other' | 'unknown' | null
  indication?: string | null
  n_countries?: number | null
  n_sites_selected?: number | null
  dropout_rate_pct?: number | null
  startup_months?: number | null
  recruitment_months?: number | null
  treatment_months?: number | null
  followup_months?: number | null
  closeout_months?: number | null
  n_expected_saes?: number | null
  n_expected_susars?: number | null
  n_monitoring_visits_onsite?: number | null
  n_monitoring_visits_remote?: number | null
  n_pharmacy_visits?: number | null
  n_visits_per_patient?: number | null
  n_protocol_amendments?: number | null
  geography_profile?: BudgetGeographyProfile | null
  geography_factor?: number | null
  archival_retention_years?: number | null
  bicr_required?: boolean | null
  comparator_sourced_by_sponsor?: boolean | null
  comparator_arm_fraction?: number | null
}

export interface BudgetCategoryCreatePayload {
  name: string
  bucket: BudgetBucket
  note?: string | null
  /** Omit to append to the end. */
  sort_order?: number | null
}

export interface BudgetCategoryUpdatePayload {
  name?: string
  bucket?: BudgetBucket
  note?: string | null
  sort_order?: number
}

/** THE TOTAL RULE, encoded:
 *  - omit `total` entirely + send both quantity and unit_cost -> server
 *    computes total = quantity * unit_cost;
 *  - send `total` (number OR null) -> server stores exactly that, forever, and
 *    never recomputes it. Use for lump-sum rows.
 *  `source` / `derivation_key` are ignored if sent; created rows are always
 *  source:"manual". */
export interface BudgetLineItemCreatePayload {
  label: string
  quantity?: number | null
  unit_cost?: number | null
  unit_description?: string | null
  total?: number | null
  status?: BudgetLineStatus
  note?: string | null
  sort_order?: number | null
}

/** Same total rule: total recomputes ONLY when `total` is omitted AND
 *  quantity and/or unit_cost are sent AND both merged values end up non-null.
 *  `category_id` moves the row to another category of the SAME budget.
 *  `source` and `derivation_key` are NOT patchable — omit them always. */
export interface BudgetLineItemUpdatePayload {
  label?: string
  quantity?: number | null
  unit_cost?: number | null
  unit_description?: string | null
  total?: number | null
  status?: BudgetLineStatus
  note?: string | null
  sort_order?: number
  category_id?: string
}

export interface BudgetReorderPayload {
  /** MUST list every sibling id exactly once, or the server returns 422. */
  ordered_ids: string[]
}

// ── Create-flow helper endpoints (§7.4) ───────────────────────────────────

/** GET /api/budgets/profile-preview?drug_inn=… — cheap, read-only, no DB
 *  write, no AI call. Powers the create-view's inline "here's what we'll
 *  build" hint while the user is still just typing a drug name. */
export interface BudgetProfilePreview {
  drug_inn: string
  modality: DrugModality
  allowed_study_types: StudyType[]
  study_type: StudyType
  subject_profile: BudgetSubjectProfile
  subject_profile_source: 'inferred_from_safety_profile' | 'default'
  subject_profile_basis: string
  therapeutic_area: string | null
  profile_key: string | null
  defaults: {
    n_enrolled: number
    n_sites: number
    n_countries?: number
    project_duration_months: number
    startup_months?: number
    recruitment_months?: number
    treatment_months?: number
    followup_months?: number
    closeout_months?: number
    n_expected_saes?: number
    geography_profile?: BudgetGeographyProfile
  }
  estimated_grand_total: number
  category_count: number
}

/** GET /api/budgets/rate-card-meta?subject_profile=… — static metadata for
 *  building UI (category list, buckets, provenance-tier copy, geography
 *  factors) without hardcoding the rate card client-side. */
export interface BudgetRateCardMeta {
  subject_profile: BudgetSubjectProfile
  rate_card_version: string
  currency: string
  disclaimer: string
  categories: { name: string; bucket: BudgetBucket; sort_order: number; line_count: number }[]
  buckets: BudgetBucket[]
  provenance_tiers: Record<BudgetProvenanceTier, string>
  geography_profiles: Record<BudgetGeographyProfile, number>
}

// ── Display maps (used by the budget components) ─────────────────────────

export const BUDGET_LINE_STATUS_LABELS: Record<BudgetLineStatus, string> = {
  costed: 'Costed',
  out_of_scope: 'Out of scope',
  not_applicable: 'Not applicable',
  not_planned: 'Not planned',
  outsourced: 'Outsourced',
  actuals_only: 'Actuals only',
}

export const BUDGET_BUCKET_LABELS: Record<BudgetBucket, string> = {
  direct: 'Direct cost',
  stipend: 'Subject stipend',
  variable: 'Variable',
  pass_through: 'Pass-through',
  investigator_grants: 'Investigator grant',
  software: 'Software / licensing',
}
