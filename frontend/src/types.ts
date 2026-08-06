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
export type RenalStatus = 'normal' | 'mild' | 'moderate' | 'severe'
export type HepaticStatus = 'normal' | 'mild' | 'moderate' | 'severe'

export interface PopulationConfig {
  subject_type: SubjectType
  age_min: number
  age_max: number
  sex: SexType
  weight_min: number
  weight_max: number
  renal_status: RenalStatus
  hepatic_status: HepaticStatus
  exclusion_presets: string[]
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
}

export const defaultPopulationConfig: PopulationConfig = {
  subject_type: 'healthy',
  age_min: 18,
  age_max: 55,
  sex: 'both',
  weight_min: 50,
  weight_max: 100,
  renal_status: 'normal',
  hepatic_status: 'normal',
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
