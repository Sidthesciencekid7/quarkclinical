import type { AdminOrgRow, AppUser, Comment, CoverageReport, CsrInputKind, CsrQCReport, DesignPreviewResponse, Drug, GeneratedProtocol, GuidelinesResponse, Lead, LeadCreatePayload, Org, OrgMember, PopulationConfig, ProtocolCreatePayload, ProtocolSummary, QCReport, RiskScore, SectionUpdateResult, StudyReport, StudyReportSummary, StudyType } from '../types'
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

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}/api${path}`, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...(await authHeaders()),
      ...(init?.headers as Record<string, string> | undefined),
    },
  })
  if (!res.ok) {
    let message = `${res.status} ${res.statusText}`
    try {
      const body = await res.json()
      if (body.detail) message = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail)
    } catch { /* ignore parse errors */ }
    throw new Error(message)
  }
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
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`
    try {
      const b = await res.json()
      if (b.detail) detail = typeof b.detail === 'string' ? b.detail : JSON.stringify(b.detail)
    } catch { /* ignore */ }
    throw new Error(detail)
  }
  return res.blob()
}

/** GET a binary payload (template / export download) with auth headers. */
async function requestBlobGet(path: string): Promise<Blob> {
  const res = await fetch(`${API_BASE}/api${path}`, { headers: { ...(await authHeaders()) } })
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`
    try {
      const b = await res.json()
      if (b.detail) detail = typeof b.detail === 'string' ? b.detail : JSON.stringify(b.detail)
    } catch { /* ignore */ }
    throw new Error(detail)
  }
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
    if (lloq !== undefined && lloq !== null && !Number.isNaN(lloq)) form.append('lloq', String(lloq))
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
