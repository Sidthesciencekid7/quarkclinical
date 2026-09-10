import type { AdminOrgRow, AppUser, Budget, BudgetCategory, BudgetCategoryCreatePayload, BudgetCategoryUpdatePayload, BudgetCreatePayload, BudgetLineItem, BudgetLineItemCreatePayload, BudgetLineItemUpdatePayload, BudgetProfilePreview, BudgetRateCardMeta, BudgetRollup, BudgetSubjectProfile, BudgetSummary, BudgetUpdatePayload, Comment, CoverageReport, CsrInputKind, CsrQCReport, DdiScreeningResult, DesignPreviewResponse, Drug, GeneratedProtocol, GuidelinesResponse, Lead, LeadCreatePayload, Org, OrgMember, PopulationConfig, ProtocolCreatePayload, ProtocolSummary, QCReport, RiskScore, SectionUpdateResult, StudyReport, StudyReportSummary, StudyType } from '../types'
import { getActingUserId } from '../lib/actingUser'

/** API origin. Empty by default so calls are same-origin `/api/...`:
 *  - dev: Vite proxies /api -> localhost:8000 (see vite.config.ts)
 *  - prod (recommended): Vercel rewrites /api -> Railway backend (see vercel.json)
 *  Set VITE_API_BASE_URL only for a direct cross-origin backend (then the backend
 *  must allow this origin via ALLOWED_ORIGINS). Trailing slash is trimmed. */
const API_BASE = (import.meta.env.VITE_API_BASE_URL as string | undefined)?.replace(/\/$/, '') ?? ''

/** Fetch a fresh Clerk session token (if signed in) for the Authorization
 *  header, plus the currently-selected "acting as" user for X-User-Id (see
 *  lib/actingUser.tsx — the backend's role checks and audit trail read this,
 *  not the Clerk token, since the two identity systems aren't linked yet). */
async function authHeaders(): Promise<Record<string, string>> {
  const headers: Record<string, string> = {}

  const actingUserId = getActingUserId()
  if (actingUserId) headers['X-User-Id'] = actingUserId

  const session = window.Clerk?.session
  if (session) {
    try {
      const token = await session.getToken()
      if (token) headers.Authorization = `Bearer ${token}`
    } catch {
      /* no session token available — X-User-Id (if any) still goes through */
    }
  }

  return headers
}

/** An HTTP failure that kept its status code. Still a plain `Error` to every
 *  existing `catch (e) { e instanceof Error ? e.message : ... }` caller — the
 *  status is additive, used only where the code itself carries meaning (e.g.
 *  telling "this route does not exist on this build" apart from a real error). */
export class HttpError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.name = 'HttpError'
    this.status = status
  }
}

/** Statuses that mean "the server has no such route", as opposed to a genuine
 *  application error. Used to probe optional endpoints (see requestBlobAny). */
const ROUTE_MISSING_STATUSES = new Set([404, 405])

/** Read the backend's `detail` out of an error response, falling back to the
 *  status line when the body isn't the usual FastAPI JSON envelope. */
async function errorFrom(res: Response): Promise<HttpError> {
  let detail = `${res.status} ${res.statusText}`
  try {
    const body = await res.json()
    if (body.detail) detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail)
  } catch { /* ignore parse errors */ }
  return new HttpError(res.status, detail)
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}/api${path}`, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...(await authHeaders()),
      ...(init?.headers as Record<string, string> | undefined),
    },
  })
  if (!res.ok) throw await errorFrom(res)
  if (res.status === 204) return undefined as T  // no body (e.g. DELETE)
  return res.json() as Promise<T>
}

async function requestBlob(path: string, body: unknown): Promise<Blob> {
  const res = await fetch(`${API_BASE}/api${path}`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(await authHeaders()),
    },
    body: JSON.stringify(body),
  })
  if (!res.ok) throw await errorFrom(res)
  return res.blob()
}

/** POST for a binary payload, trying each candidate path until one exists.
 *
 *  The standalone study-package documents (ICF, randomisation list, CRF/DMP)
 *  are rendered by export endpoints whose exact names are owned by the backend.
 *  Only the ICF pair is spelled out in the engine's integration notes
 *  (POST /icf/docx, /icf/pdf under the export router); for the other two the
 *  notes describe the payload but not the route, so both plausible spellings
 *  are probed and the first that is registered wins. A 404/405 means "not this
 *  name"; any other status is a real failure and is surfaced immediately.
 *
 *  These are sub-paths, not collection roots, so they take NO trailing slash —
 *  same as the existing /export/docx and /export/pdf. (A trailing slash on a
 *  route registered without one triggers a cross-origin 307 that silently drops
 *  the Authorization header.) */
async function requestBlobAny(paths: string[], body: unknown): Promise<Blob> {
  let lastError: unknown
  for (const path of paths) {
    try {
      return await requestBlob(path, body)
    } catch (e) {
      lastError = e
      if (e instanceof HttpError && ROUTE_MISSING_STATUSES.has(e.status)) continue
      throw e
    }
  }
  throw lastError instanceof Error ? lastError : new Error('Export failed.')
}

/** GET a binary payload (template / export download) with auth headers. */
async function requestBlobGet(path: string): Promise<Blob> {
  const res = await fetch(`${API_BASE}/api${path}`, { headers: { ...(await authHeaders()) } })
  if (!res.ok) throw await errorFrom(res)
  return res.blob()
}

/** Error carrying the backend's per-row dataset validation messages. */
export class DatasetValidationError extends Error {
  errors: string[]
  constructor(message: string, errors: string[]) {
    super(message)
    this.name = 'DatasetValidationError'
    this.errors = errors
  }
}

/** Wire shape of a review comment as the backend returns it (schemas.py::CommentRead). */
interface CommentWire {
  id: string
  protocol_id: string
  section_name: string
  text: string
  author_name: string
  resolved: boolean
  created_at: string
}

function toComment(row: CommentWire): Comment {
  return {
    id: row.id,
    sectionName: row.section_name,
    text: row.text,
    author: row.author_name,
    // Postgres timestamptz serialises without a zone suffix in some setups;
    // append Z so Date() treats it as UTC rather than local time.
    createdAt: /(Z|[+-]\d{2}:?\d{2})$/.test(row.created_at) ? row.created_at : `${row.created_at}Z`,
    resolved: row.resolved,
  }
}

export const api = {
  listUsers: () => request<AppUser[]>('/users/'),

  /** The backend-resolved current user. With Clerk auth on, this is derived
   *  from the VERIFIED session (Clerk email → users row), so it's the real,
   *  spoof-proof identity — not whatever the "acting as" picker selected. */
  getCurrentUser: () => request<AppUser>('/users/me'),

  searchDrugs: (q: string) =>
    request<Drug[]>(`/drugs/?q=${encodeURIComponent(q)}`),

  listGuidelines: () => request<GuidelinesResponse>('/guidelines/'),

  createDrug: (inn: string) =>
    request<Drug>('/drugs/', {
      method: 'POST',
      body: JSON.stringify({ inn: inn.trim().toLowerCase(), brand_names: [] }),
    }),

  createProtocol: (payload: ProtocolCreatePayload) =>
    request<GeneratedProtocol>('/protocols/', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  /** Credit-free preview of the 5 study-design decisions (Review design step). */
  designPreview: (drugId: string, studyType: StudyType, populationConfig: PopulationConfig) =>
    request<DesignPreviewResponse>('/protocols/design-preview', {
      method: 'POST',
      body: JSON.stringify({ drug_id: drugId, study_type: studyType, population_config: populationConfig }),
    }),

  /** Credit-free drug-drug-interaction screening for the Population step.
   *
   *  The backend reads the drug's real DailyMed label, works out which
   *  metabolic/transporter pathways actually apply, and returns the
   *  comedication rows that belong in the exclusion criteria. This is what
   *  replaces the frontend's old static "CYP3A4 inhibitors or inducers"
   *  preset, which was shown for every drug regardless of its pathway.
   *
   *  The screening is computed server-side at protocol creation; which route
   *  exposes it to the wizard is the backend's call, so both plausible surfaces
   *  are supported: a dedicated /protocols/ddi-preview, or a `ddi` field
   *  attached to the existing design-preview response. Resolves to null when
   *  neither is available, and the caller then falls back to the drug-agnostic
   *  criteria only — it must never invent pathway-specific ones itself. */
  previewDdi: async (
    drugId: string,
    studyType: StudyType,
    populationConfig: PopulationConfig,
  ): Promise<DdiScreeningResult | null> => {
    const body = JSON.stringify({
      drug_id: drugId,
      study_type: studyType,
      population_config: populationConfig,
    })
    try {
      return await request<DdiScreeningResult>('/protocols/ddi-preview', { method: 'POST', body })
    } catch (e) {
      if (!(e instanceof HttpError) || !ROUTE_MISSING_STATUSES.has(e.status)) throw e
    }
    const preview = await request<DesignPreviewResponse>('/protocols/design-preview', { method: 'POST', body })
    return preview.ddi ?? null
  },

  /** The caller's org's past protocols, newest first (My Reports history). */
  listProtocols: () => request<ProtocolSummary[]>('/protocols/'),

  /** Full protocol (with sections) — used to re-open a past report in the editor. */
  getProtocol: (id: string) => request<GeneratedProtocol>(`/protocols/${id}`),

  getQC: (protocolId: string) =>
    request<QCReport>(`/protocols/${protocolId}/qc`),

  getCoverage: (protocolId: string) =>
    request<CoverageReport>(`/protocols/${protocolId}/coverage`),

  exportDocx: (protocolId: string): Promise<Blob> =>
    requestBlob('/export/docx', { protocol_id: protocolId }),

  exportPdf: (protocolId: string): Promise<Blob> =>
    requestBlob('/export/pdf', { protocol_id: protocolId }),

  // ── Standalone study-package documents ─────────────────────────────────
  // These are SEPARATE documents from the protocol, not sections of it:
  //  - the ICF is an IRB submission carrying its own version and date
  //    (21 CFR 50.25), which is the legal point of keeping it separate;
  //  - the randomisation list is the RESTRICTED unblinded allocation and must
  //    never be filed in the protocol body (ICH E9 allocation concealment);
  //  - the CRF specification + Data Management Plan is the data-capture
  //    appendix pair.
  // Each takes the same {protocol_id} body as the protocol export endpoints.

  exportIcfDocx: (protocolId: string): Promise<Blob> =>
    requestBlobAny(['/export/icf/docx'], { protocol_id: protocolId }),

  exportIcfPdf: (protocolId: string): Promise<Blob> =>
    requestBlobAny(['/export/icf/pdf'], { protocol_id: protocolId }),

  exportCrfDmpDocx: (protocolId: string): Promise<Blob> =>
    requestBlobAny(['/export/crf/docx', '/export/crf-dmp/docx'], { protocol_id: protocolId }),

  exportCrfDmpPdf: (protocolId: string): Promise<Blob> =>
    requestBlobAny(['/export/crf/pdf', '/export/crf-dmp/pdf'], { protocol_id: protocolId }),

  /** RESTRICTED: the unblinded subject-by-subject allocation, as an ASCII CSV
   *  for a separate TMF appendix. Never part of the protocol document. */
  exportRandomizationCsv: (protocolId: string): Promise<Blob> =>
    requestBlobAny(
      ['/export/randomization/csv', '/export/randomization-list/csv'],
      { protocol_id: protocolId },
    ),

  // ── Clinical Study Report (CSR) — post-study results + ICH E3 narrative ──

  /** Download the fixed data template to fill in ('nca' | 'params' | 'ae'). */
  downloadCsrTemplate: (kind: string, fmt: 'csv' | 'xlsx' = 'csv'): Promise<Blob> =>
    requestBlobGet(`/study-reports/template?kind=${kind}&fmt=${fmt}`),

  /** CSRs already created for a protocol (newest first). */
  listStudyReports: (protocolId: string) =>
    request<StudyReportSummary[]>(`/protocols/${protocolId}/study-reports`),

  /** Create the CSR shell bound to a completed protocol. */
  createStudyReport: (protocolId: string, inputKind: CsrInputKind) =>
    request<StudyReport>(`/protocols/${protocolId}/study-report`, {
      method: 'POST',
      body: JSON.stringify({ input_kind: inputKind }),
    }),

  getStudyReport: (id: string) => request<StudyReport>(`/study-reports/${id}`),

  /** Upload the results file (multipart) → deterministic analysis → locked results.
   *  Surfaces the backend's per-row validation errors via DatasetValidationError. */
  uploadStudyReportDataset: async (id: string, file: File, lloq?: number | null): Promise<StudyReport> => {
    const form = new FormData()
    form.append('file', file)
    if (lloq !== undefined && lloq !== null && Number.isFinite(lloq) && lloq > 0) form.append('lloq', String(lloq))
    const res = await fetch(`${API_BASE}/api/study-reports/${id}/dataset`, {
      method: 'POST',
      // No Content-Type — the browser sets the multipart boundary itself.
      headers: { ...(await authHeaders()) },
      body: form,
    })
    if (!res.ok) {
      let message = `${res.status} ${res.statusText}`
      let errors: string[] | undefined
      try {
        const b = await res.json()
        if (b.detail) {
          if (typeof b.detail === 'string') message = b.detail
          else { message = b.detail.message || message; errors = b.detail.errors }
        }
      } catch { /* ignore */ }
      if (errors && errors.length) throw new DatasetValidationError(message, errors)
      throw new Error(message)
    }
    return res.json() as Promise<StudyReport>
  },

  /** Kick off the ICH E3 narrative generation (async in prod; poll getStudyReport). */
  generateStudyReport: (id: string) =>
    request<StudyReport>(`/study-reports/${id}/generate`, { method: 'POST' }),

  getStudyReportQC: (id: string) => request<CsrQCReport>(`/study-reports/${id}/qc`),

  exportCsrDocx: (id: string): Promise<Blob> => requestBlobGet(`/study-reports/${id}/export/docx`),
  exportCsrPdf: (id: string): Promise<Blob> => requestBlobGet(`/study-reports/${id}/export/pdf`),

  regenSection: (
    protocolId: string,
    sectionName: string,
    contextOverride: Record<string, unknown> = {},
  ) =>
    request<SectionUpdateResult>(
      `/protocols/${protocolId}/sections/${sectionName}`,
      {
        method: 'PATCH',
        body: JSON.stringify({ context_override: contextOverride }),
      },
    ),

  saveSection: (protocolId: string, sectionName: string, content: string) =>
    request<SectionUpdateResult>(
      `/protocols/${protocolId}/sections/${sectionName}`,
      {
        method: 'PUT',
        body: JSON.stringify({ content }),
      },
    ),

  // ── Organization / license / team ──────────────────────────────────────
  // `email` is only used when Clerk auth is disabled (local dev/tests) — the
  // backend ignores it and derives the email from the verified session
  // whenever a real Clerk session is present.

  createOrg: (name: string, email?: string) =>
    request<Org>('/orgs/', {
      method: 'POST',
      body: JSON.stringify({ name, email }),
    }),

  joinOrg: (token: string, email?: string) =>
    request<Org>('/orgs/join', {
      method: 'POST',
      body: JSON.stringify({ token, email }),
    }),

  getMyOrg: () => request<Org>('/orgs/me'),

  rotateJoinToken: () => request<Org>('/orgs/rotate-join-token', { method: 'POST' }),

  listOrgMembers: () => request<OrgMember[]>('/orgs/members'),

  // ── Leads ("Contact sales") ──────────────────────────────────────────
  // Public — no auth headers required, matches the backend route.

  submitLead: (payload: LeadCreatePayload) =>
    request<{ id: string }>('/leads/', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  // ── Admin dashboard (ADMIN_EMAILS-gated on the backend) ──────────────

  listLeads: () => request<Lead[]>('/leads/'),

  // ── Section review comments (persistent + org-shared) ────────────────
  // The API speaks snake_case; the editor's Comment type is camelCase, so the
  // mapping lives here rather than leaking wire shapes into components.

  listComments: async (protocolId: string): Promise<Comment[]> => {
    const rows = await request<CommentWire[]>(`/protocols/${protocolId}/comments`)
    return rows.map(toComment)
  },

  createComment: async (protocolId: string, sectionName: string, text: string): Promise<Comment> =>
    toComment(
      await request<CommentWire>(`/protocols/${protocolId}/comments`, {
        method: 'POST',
        body: JSON.stringify({ section_name: sectionName, text }),
      }),
    ),

  updateComment: async (protocolId: string, commentId: string, resolved: boolean): Promise<Comment> =>
    toComment(
      await request<CommentWire>(`/protocols/${protocolId}/comments/${commentId}`, {
        method: 'PATCH',
        body: JSON.stringify({ resolved }),
      }),
    ),

  deleteComment: (protocolId: string, commentId: string) =>
    request<void>(`/protocols/${protocolId}/comments/${commentId}`, { method: 'DELETE' }),

  deleteLead: (leadId: string) =>
    request<void>(`/leads/${leadId}`, { method: 'DELETE' }),

  /** Deterministic operational feasibility risk score (no AI call). */
  getProtocolRisk: (protocolId: string) =>
    request<RiskScore>(`/protocols/${protocolId}/risk`),

  adminListOrgs: () => request<AdminOrgRow[]>('/orgs/admin/list'),

  // ── Budgeting Tool ───────────────────────────────────────────────────
  // Org-scoped like everything else; no org id is ever sent. The server owns
  // ALL budget arithmetic — after any write, re-read (getBudget) or refresh
  // totals only (getBudgetRollup). Never sum on the client.

  /** This org's budgets, newest-modified first (My Budgets list). */
  listBudgets: () => request<BudgetSummary[]>('/budgets'),

  /** Create a budget. `protocol_id` is OPTIONAL — omitting it is a
   *  first-class standalone flow, not a degraded one. */
  createBudget: (payload: BudgetCreatePayload) =>
    request<Budget>('/budgets', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  /** Full budget: categories, line items and a freshly recomputed rollup. */
  getBudget: (budgetId: string) => request<Budget>(`/budgets/${budgetId}`),

  /** Patch budget-level fields. Omitted key = unchanged, explicit null =
   *  cleared. `protocol_id` is not accepted here — use linkBudgetProtocol. */
  updateBudget: (budgetId: string, payload: BudgetUpdatePayload) =>
    request<Budget>(`/budgets/${budgetId}`, {
      method: 'PATCH',
      body: JSON.stringify(payload),
    }),

  deleteBudget: (budgetId: string) =>
    request<void>(`/budgets/${budgetId}`, { method: 'DELETE' }),

  /** Link, re-link (re-derives) or unlink (pass null) a protocol.
   *  Re-linking updates derived quantities in place and NEVER overwrites a
   *  unit_cost or total the user typed; unlinking KEEPS the line items. */
  linkBudgetProtocol: (budgetId: string, protocolId: string | null) =>
    request<Budget>(`/budgets/${budgetId}/link-protocol`, {
      method: 'POST',
      body: JSON.stringify({ protocol_id: protocolId }),
    }),

  /** Cheap totals-only refresh for the sticky footer after an inline edit.
   *  Also carries category_subtotals, so one call refreshes every aggregate
   *  on screen. */
  getBudgetRollup: (budgetId: string) =>
    request<BudgetRollup>(`/budgets/${budgetId}/rollup`),

  /** The budget as a Word document: cover page, 1.0 Assumptions, 2.0 Summary
   *  Study Budget, 3.0 Itemized Study Budget, 4.0 Basis of Estimate. GET, no
   *  body — same shape as exportCsrDocx. */
  exportBudgetDocx: (budgetId: string): Promise<Blob> =>
    requestBlobGet(`/budgets/${budgetId}/export/docx`),

  /** Re-prices every auto-priced, still-untouched line against the budget's
   *  CURRENT drivers (n_sites, the five phase durations, etc.) and the
   *  profile-appropriate rate card. A plain PATCH never moves a total — call
   *  this after editing a footprint/timeline field so the numbers don't go
   *  stale. Body is always `{}`. Returns the full budget. */
  repriceBudget: (budgetId: string) =>
    request<Budget>(`/budgets/${budgetId}/reprice`, { method: 'POST', body: JSON.stringify({}) }),

  /** Cheap, read-only, no DB write, no AI call — powers the create-view's
   *  inline "here's what we'll build" hint while the user is still just
   *  typing a drug name. `drugInn` may be partial/blank; the caller should
   *  debounce (~400ms) and ignore a blank query rather than calling this. */
  budgetProfilePreview: (drugInn: string) =>
    request<BudgetProfilePreview>(`/budgets/profile-preview?drug_inn=${encodeURIComponent(drugInn)}`),

  /** Static rate-card metadata (category list, buckets, provenance-tier
   *  copy, geography factors) for building UI without hardcoding the card. */
  budgetRateCardMeta: (subjectProfile: BudgetSubjectProfile) =>
    request<BudgetRateCardMeta>(`/budgets/rate-card-meta?subject_profile=${subjectProfile}`),

  createBudgetCategory: (budgetId: string, payload: BudgetCategoryCreatePayload) =>
    request<BudgetCategory>(`/budgets/${budgetId}/categories`, {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  updateBudgetCategory: (
    budgetId: string,
    categoryId: string,
    payload: BudgetCategoryUpdatePayload,
  ) =>
    request<BudgetCategory>(`/budgets/${budgetId}/categories/${categoryId}`, {
      method: 'PATCH',
      body: JSON.stringify(payload),
    }),

  /** Deletes the category's line items too. */
  deleteBudgetCategory: (budgetId: string, categoryId: string) =>
    request<void>(`/budgets/${budgetId}/categories/${categoryId}`, {
      method: 'DELETE',
    }),

  /** `orderedIds` MUST list every category of this budget exactly once (422
   *  otherwise). Returns the categories in the new order. */
  reorderBudgetCategories: (budgetId: string, orderedIds: string[]) =>
    request<BudgetCategory[]>(`/budgets/${budgetId}/categories/reorder`, {
      method: 'POST',
      body: JSON.stringify({ ordered_ids: orderedIds }),
    }),

  /** Create a line item. Omit `total` and pass both quantity and unit_cost to
   *  let the server multiply; pass `total` (number or null) for a lump sum the
   *  server stores verbatim and never recomputes. */
  createBudgetLineItem: (
    budgetId: string,
    categoryId: string,
    payload: BudgetLineItemCreatePayload,
  ) =>
    request<BudgetLineItem>(`/budgets/${budgetId}/categories/${categoryId}/line-items`, {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  /** NOTE the route shape: line items are flat under the BUDGET, not under
   *  the category. Send `category_id` in the payload to move a row. */
  updateBudgetLineItem: (
    budgetId: string,
    lineItemId: string,
    payload: BudgetLineItemUpdatePayload,
  ) =>
    request<BudgetLineItem>(`/budgets/${budgetId}/line-items/${lineItemId}`, {
      method: 'PATCH',
      body: JSON.stringify(payload),
    }),

  deleteBudgetLineItem: (budgetId: string, lineItemId: string) =>
    request<void>(`/budgets/${budgetId}/line-items/${lineItemId}`, {
      method: 'DELETE',
    }),

  /** Same all-ids-exactly-once rule as reorderBudgetCategories. */
  reorderBudgetLineItems: (budgetId: string, categoryId: string, orderedIds: string[]) =>
    request<BudgetLineItem[]>(
      `/budgets/${budgetId}/categories/${categoryId}/line-items/reorder`,
      { method: 'POST', body: JSON.stringify({ ordered_ids: orderedIds }) },
    ),

  // ── Clone / start-from-template ──────────────────────────────────────
  /**
   * Duplicate an existing protocol as a fresh draft ("start from template").
   * The backend copies the already-generated sections verbatim — the AI
   * pipeline is NOT re-run — so this returns instantly and costs no credits.
   * Resolves to the NEW protocol (fresh id, version 1, status "draft").
   */
  cloneProtocol: (protocolId: string) =>
    request<GeneratedProtocol>(`/protocols/${protocolId}/clone`, { method: 'POST' }),

  activateLicense: (orgId: string, opts: { seat_count?: number; period_days?: number } = {}) =>
    request<Org>(`/orgs/${orgId}/activate-license`, {
      method: 'POST',
      body: JSON.stringify(opts),
    }),

  suspendLicense: (orgId: string) =>
    request<Org>(`/orgs/${orgId}/suspend-license`, { method: 'POST' }),
}
