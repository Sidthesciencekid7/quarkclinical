import type { ActionResponse, Card, CardType, ChatMessageDisplay, ChatSessionDetail, SpecSummary } from '../types'

export const SESSION_ID = '11111111-1111-4111-8111-111111111111'

export function spec(overrides: Partial<SpecSummary> = {}): SpecSummary {
  return {
    kind: 'protocol',
    spec_version: 3,
    ready: false,
    missing: ['drug'],
    fields: [
      { path: 'drug', label: 'Drug', display_value: null, status: 'empty', source: null },
      { path: 'guideline_version', label: 'Guideline', display_value: 'FDA-BE-2014', status: 'default', source: 'default' },
    ],
    ...overrides,
  }
}

export function card(type: CardType, data: Record<string, unknown>, overrides: Partial<Card> = {}): Card {
  return {
    id: `cccccccc-0000-4000-8000-${String(Math.abs(hash(type + JSON.stringify(data)))).padStart(12, '0').slice(0, 12)}`,
    type,
    status: 'active',
    spec_version: 3,
    title: type.replace(/_/g, ' '),
    data,
    actions: [],
    ...overrides,
  }
}

function hash(s: string): number {
  let h = 0
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0
  return h
}

export function message(over: Partial<ChatMessageDisplay> & Pick<ChatMessageDisplay, 'id' | 'role' | 'text'>): ChatMessageDisplay {
  return { seq: 1, card_ids: [], flags: { unverified_numbers: [] }, created_at: '2026-10-24T10:00:00Z', ...over }
}

export function detail(over: Partial<ChatSessionDetail> = {}): ChatSessionDetail {
  return {
    session: {
      id: SESSION_ID,
      kind: 'protocol',
      status: 'active',
      title: null,
      spec_version: 3,
      protocol_id: null,
      budget_id: null,
      created_at: '2026-10-24T10:00:00Z',
      updated_at: '2026-10-24T10:00:00Z',
      last_activity_at: '2026-10-24T10:00:00Z',
    },
    messages: [],
    cards: [],
    spec: spec(),
    awaiting_reply: false,
    ...over,
  }
}

export function actionResponse(over: Partial<ActionResponse> = {}): ActionResponse {
  const s = over.spec ?? spec({ spec_version: 4 })
  return { spec_version: s.spec_version, spec: s, cards: [], guide: { text: 'Next: pick the study type.', chips: [] }, ...over }
}

export const DRUG_MATCHES = (): Card =>
  card('drug_matches', {
    query: 'adalimumab',
    matches: [
      { id: '22222222-2222-4222-8222-222222222222', inn: 'adalimumab', brand_names: ['Humira'], modality: 'biologic', allowed_study_types: ['biosimilar_pk'] },
    ],
    truncated: false,
    selected_drug_id: null,
  })
