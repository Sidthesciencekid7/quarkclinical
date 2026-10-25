import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { Card, CardAction } from '../types'
import { DRUG_MATCHES, card } from './fixtures'

const h = vi.hoisted(() => ({ getProtocol: vi.fn(), getProposal: vi.fn() }))

vi.mock('../../api/client', async importOriginal => {
  const actual = await importOriginal<typeof import('../../api/client')>()
  return { ...actual, api: { ...actual.api, getProtocol: h.getProtocol } }
})
vi.mock('../../api/chat', async importOriginal => {
  const actual = await importOriginal<typeof import('../../api/chat')>()
  return { ...actual, getProposal: h.getProposal }
})

import { CARD_COMPONENTS, CardContext, CardRenderer } from '../cards'
import type { CardContextValue } from '../cards'
import { payloadForCardAction } from '../cards/payloads'

function ctxValue(over: Partial<CardContextValue> = {}): CardContextValue {
  return {
    sessionId: 's1',
    kind: 'protocol',
    busy: false,
    pending: null,
    errors: {},
    cards: [],
    dispatch: vi.fn(async () => null),
    sendText: vi.fn(),
    resync: vi.fn(async () => {}),
    ...over,
  }
}

function show(c: Card, over: Partial<CardContextValue> = {}) {
  const ctx = ctxValue(over)
  const view = render(
    <MemoryRouter>
      <CardContext.Provider value={ctx}>
        <CardRenderer card={c} />
      </CardContext.Provider>
    </MemoryRouter>,
  )
  return { ctx, ...view }
}

const act = (id: string, label: string, variant: CardAction['variant'] = 'primary'): CardAction => ({ id, label, variant })

beforeEach(() => {
  h.getProtocol.mockReset()
  h.getProposal.mockReset()
})

describe('every card type has a renderer', () => {
  it('covers the whole CardType union', () => {
    expect(Object.keys(CARD_COMPONENTS).sort()).toEqual(
      [
        'budget_preview', 'budget_ready', 'budget_summary', 'cv_override_warning', 'design_decisions', 'drug_add_offer', 'drug_matches',
        'edit_diff', 'edit_impact', 'edit_progress', 'generation_progress', 'guideline', 'indication', 'interaction_screening', 'notice',
        'population', 'protocol_overview', 'protocol_ready', 'protocol_summary', 'study_type',
      ].sort(),
    )
  })
})

describe('generic fallback', () => {
  it('shows an unknown card type as a plain titled card with no buttons', () => {
    const c = card('notice', { widget: 'x', colour: 'teal', count: 3 }, { title: 'Hologram', actions: [act('generate_protocol', 'Generate')] })
    const unknown = { ...c, type: 'hologram' } as unknown as Card
    const { container } = show(unknown)
    expect(screen.getByRole('heading', { name: 'Hologram' })).toBeInTheDocument()
    expect(screen.getByText('teal')).toBeInTheDocument()
    expect(container.querySelector('button')).toBeNull()
  })

  it('falls back instead of crashing when a known card has data it cannot read', () => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {})
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const broken = card('population', { values: null }, { title: 'Participants' })
    show(broken)
    expect(screen.getByRole('heading', { name: 'Participants' })).toBeInTheDocument()
    expect(screen.getByText(/no details to show/i)).toBeInTheDocument()
    spy.mockRestore()
    warn.mockRestore()
  })
})

describe('card lifecycle', () => {
  it('dims a superseded card and disables its buttons', () => {
    const c = { ...DRUG_MATCHES(), status: 'superseded' as const }
    const { container, ctx } = show(c)
    expect(container.querySelector('article')).toHaveAttribute('data-superseded')
    expect(screen.getByText('Replaced')).toBeInTheDocument()
    const use = screen.getByRole('button', { name: 'Use adalimumab' })
    expect(use).toBeDisabled()
    fireEvent.click(use)
    expect(ctx.dispatch).not.toHaveBeenCalled()
  })

  it('makes a finished card read-only and marks it done', () => {
    show({ ...DRUG_MATCHES(), status: 'done' })
    expect(screen.getByText('Done')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Use adalimumab' })).toBeDisabled()
  })

  it('is inert while a reply is streaming', () => {
    const { ctx } = show(DRUG_MATCHES(), { busy: true })
    const use = screen.getByRole('button', { name: 'Use adalimumab' })
    expect(use).toBeDisabled()
    fireEvent.click(use)
    expect(ctx.dispatch).not.toHaveBeenCalled()
  })

  it('shows an action failure inside the card, in the server’s words', () => {
    const c = DRUG_MATCHES()
    show(c, { errors: { [c.id]: 'Biologics cannot run a BE study.' } })
    expect(screen.getByRole('alert')).toHaveTextContent('Biologics cannot run a BE study.')
  })
})

describe('card actions send the contract payloads', () => {
  it('drug_matches: Use X -> select_drug {drug_id}', () => {
    const c = DRUG_MATCHES()
    const { ctx } = show(c)
    fireEvent.click(screen.getByRole('button', { name: 'Use adalimumab' }))
    expect(ctx.dispatch).toHaveBeenCalledWith(c, 'select_drug', { drug_id: '22222222-2222-4222-8222-222222222222' })
  })

  it('drug_add_offer: the server button -> add_drug {inn}', () => {
    const c = card('drug_add_offer', { query: 'newdrug', inn: 'newdrug', valid: true, message: 'Not in the catalogue yet.', modality: 'unknown', allowed_study_types: [] }, { actions: [act('add_drug', 'Add "newdrug" to the catalogue')] })
    const { ctx } = show(c)
    fireEvent.click(screen.getByRole('button', { name: /Add "newdrug"/ }))
    expect(ctx.dispatch).toHaveBeenCalledWith(c, 'add_drug', { inn: 'newdrug' })
  })

  it('study_type: a gated option is disabled with the server’s reason; an allowed one sends set_study_type', () => {
    const c = card('study_type', {
      drug_inn: 'adalimumab',
      modality: 'biologic',
      gate_message: 'adalimumab is a biologic: only a biosimilar PK study applies.',
      selected: null,
      options: [
        { value: 'BE', label: 'Bioequivalence', allowed: false, reason: 'Biologics cannot run a BE study.' },
        { value: 'biosimilar_pk', label: 'Biosimilar PK Similarity', allowed: true, reason: null },
        { value: 'food_effect', label: 'Food Effect', allowed: false, reason: 'Not applicable to biologics.' },
      ],
    })
    const { ctx } = show(c)
    expect(screen.getByText('adalimumab is a biologic: only a biosimilar PK study applies.')).toBeInTheDocument()
    const be = screen.getByRole('button', { name: /Bioequivalence/ })
    expect(be).toBeDisabled()
    expect(screen.getByText('Biologics cannot run a BE study.')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /Biosimilar PK Similarity/ }))
    expect(ctx.dispatch).toHaveBeenCalledWith(c, 'set_study_type', { study_type: 'biosimilar_pk' })
  })

  const population = () =>
    card('population', {
      drug_inn: 'adalimumab',
      study_type: 'biosimilar_pk',
      values: {
        subject_type: 'healthy', age_min: 18, age_max: 55, sex: 'both', weight_min: 50, weight_max: 100,
        exclusion_presets: ['Tobacco or nicotine use within 6 months'], cv_intra: null, dose_strength: null,
        reference_product: null, is_nti: null, additional_pk_timepoints_hours: null, additional_immunogenicity_timepoints_months: null,
      },
      ranges: { age_min: 18, age_max: 80, weight_min: 40, weight_max: 150, cv_min: 1, cv_max: 120 },
      generic_exclusion_options: ['Tobacco or nicotine use within 6 months', 'Blood donation within 3 months'],
      field_status: { 'population.age_min': 'default' },
      confirm_patch: { subject_type: 'healthy' },
    })

  it('population: Looks good -> update_population {confirm_patch}', () => {
    const c = population()
    const { ctx } = show(c)
    fireEvent.click(screen.getByRole('button', { name: 'Looks good' }))
    expect(ctx.dispatch).toHaveBeenCalledWith(c, 'update_population', { patch: { subject_type: 'healthy' } })
  })

  it('population: Edit, change the maximum age, Save -> only the changed key is sent', () => {
    const c = population()
    const { ctx } = show(c)
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }))
    const save = screen.getByRole('button', { name: 'Save changes' })
    expect(save).toBeDisabled()
    fireEvent.change(screen.getByRole('spinbutton', { name: 'Maximum age' }), { target: { value: '45' } })
    fireEvent.click(save)
    expect(ctx.dispatch).toHaveBeenCalledWith(c, 'update_population', { patch: { age_max: 45 } })
  })

  it('population: clearing an optional field sends null for it', () => {
    const c = population()
    c.data = { ...c.data, values: { ...(c.data.values as object), cv_intra: 30 } }
    const { ctx } = show(c)
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }))
    fireEvent.change(screen.getByRole('spinbutton', { name: /Within-subject CV/ }), { target: { value: '' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save changes' }))
    expect(ctx.dispatch).toHaveBeenCalledWith(c, 'update_population', { patch: { cv_intra: null } })
  })

  it('population: an exclusion toggle -> toggle_exclusion {text, on}', () => {
    const c = population()
    const { ctx } = show(c)
    fireEvent.click(screen.getByRole('button', { name: 'Blood donation within 3 months' }))
    expect(ctx.dispatch).toHaveBeenCalledWith(c, 'toggle_exclusion', { text: 'Blood donation within 3 months', on: true })
    fireEvent.click(screen.getByRole('button', { name: 'Tobacco or nicotine use within 6 months' }))
    expect(ctx.dispatch).toHaveBeenCalledWith(c, 'toggle_exclusion', { text: 'Tobacco or nicotine use within 6 months', on: false })
  })

  it('cv_override_warning: the button clears the override', () => {
    const c = card('cv_override_warning', { cv_intra: 30, message: 'You set the CV to 30%.', consequences: ['The protocol declares it investigator-specified.'], clear_patch: { cv_intra: null } }, { actions: [act('update_population', 'Use the literature CV instead', 'secondary')] })
    const { ctx } = show(c)
    expect(screen.getByText('The protocol declares it investigator-specified.')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Use the literature CV instead' }))
    expect(ctx.dispatch).toHaveBeenCalledWith(c, 'update_population', { patch: { cv_intra: null } })
  })

  const row = (over: Record<string, unknown> = {}) => ({
    key: 'cyp3a4_inhibitors', label: 'Strong CYP3A4 inhibitors', status: 'prohibited', pathway: 'cyp3a4', examples: ['ketoconazole'],
    washout_days: 14, washout_basis: '5 half-lives', history_exclusion: false, rationale: 'Raises exposure.', conditions: null,
    escalated_by_nti: false, assumption: null, evidence_quote: 'Avoid strong CYP3A4 inhibitors.',
    criterion_text: 'Strong CYP3A4 inhibitors (e.g. ketoconazole) within 14 days before first dose', selected: false, ...over,
  })

  it('interaction_screening: a row toggle -> toggle_exclusion with the server’s criterion text', () => {
    const c = card('interaction_screening', { drug_inn: 'ibuprofen', design_key: 'crossover', is_nti: false, pathway_keys: ['cyp3a4'], prohibited: [row()], restricted: [], permitted: [], max_washout_days: 14, screening_history_window_days: null, unresolved_count: 0, summary: null })
    const { ctx } = show(c)
    expect(screen.getByText('CYP3A4')).toBeInTheDocument()
    expect(screen.getByText(/Longest washout: 14 days/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /Strong CYP3A4 inhibitors/ }))
    expect(ctx.dispatch).toHaveBeenCalledWith(c, 'toggle_exclusion', { text: 'Strong CYP3A4 inhibitors (e.g. ketoconazole) within 14 days before first dose', on: true })
  })

  it('interaction_screening: a selected row toggles off; the label evidence is shown as quoted data', () => {
    const c = card('interaction_screening', { drug_inn: 'ibuprofen', pathway_keys: ['cyp3a4'], prohibited: [row({ selected: true })], restricted: [], permitted: [], unresolved_count: 0 })
    const { ctx } = show(c)
    const toggle = screen.getByRole('button', { name: /Strong CYP3A4 inhibitors/ })
    expect(toggle).toHaveAttribute('aria-pressed', 'true')
    fireEvent.click(toggle)
    expect(ctx.dispatch).toHaveBeenCalledWith(c, 'toggle_exclusion', expect.objectContaining({ on: false }))
    expect(screen.getByText(/Label evidence: "Avoid strong CYP3A4 inhibitors."/)).toBeInTheDocument()
  })

  const design = (confirmed = false) =>
    card(
      'design_decisions',
      {
        drug_inn: 'adalimumab', study_type: 'biosimilar_pk', confirmed,
        decisions: [
          { key: 'population', label: 'Population', value: 'Healthy volunteers', rationale: 'A single dose is safe.', evidence: '', options: ['Healthy volunteers', 'Patients · multi-dose'] },
          { key: 'route', label: 'Route', value: 'SC, prefilled syringe, abdomen', rationale: 'Matches the label.', evidence: '', options: ['SC, prefilled syringe, abdomen'] },
        ],
      },
      { actions: confirmed ? [] : [act('confirm_design', 'Confirm design')] },
    )

  it('design_decisions: changing a decision -> set_design_decision; the button -> confirm_design', () => {
    const c = design()
    const { ctx } = show(c)
    fireEvent.change(screen.getByRole('combobox', { name: 'Population' }), { target: { value: 'Patients · multi-dose' } })
    expect(ctx.dispatch).toHaveBeenCalledWith(c, 'set_design_decision', { key: 'population', value: 'Patients · multi-dose' })
    // A single-option decision is fixed text, not a control.
    expect(screen.queryByRole('combobox', { name: 'Route' })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Confirm design' }))
    expect(ctx.dispatch).toHaveBeenCalledWith(c, 'confirm_design', {})
  })

  const indication = () =>
    card(
      'indication',
      {
        applicable: true, reason: null, drug_inn: 'nivolumab', drug_class: 'PD-1 inhibitor', label_verified: true,
        recommended: { indication: 'Resected melanoma (adjuvant)', on_label: true, label_evidence: null, therapeutic_area: 'oncology', setting_note: 'Adjuvant setting', monotherapy: 'yes', pk_variability: 'low', observed_cv_pct: 22, recruitment: 'good', rank_reason: 'On label and lowest burden.' },
        ranked: [
          { indication: 'Resected melanoma (adjuvant)', on_label: true, label_evidence: null, therapeutic_area: 'oncology', setting_note: 'Adjuvant setting', monotherapy: 'yes', pk_variability: 'low', observed_cv_pct: 22, recruitment: 'good', rank_reason: 'On label and lowest burden.' },
          { indication: 'Metastatic melanoma', on_label: true, label_evidence: null, therapeutic_area: 'oncology', setting_note: null, monotherapy: 'possible', pk_variability: 'high', observed_cv_pct: null, recruitment: null, rank_reason: null },
        ],
        user_indication: null, user_indication_warnings: ['Not found on the label.'], selected_indication: 'Resected melanoma (adjuvant)', summary: null,
      },
      { actions: [act('set_indication', 'Let the engine choose', 'secondary')] },
    )

  it('indication: Use this / a typed indication / Let the engine choose all use set_indication', () => {
    const c = indication()
    const { ctx } = show(c)
    expect(screen.getByText('Not found on the label.')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Use this indication: Metastatic melanoma' }))
    expect(ctx.dispatch).toHaveBeenCalledWith(c, 'set_indication', { text: 'Metastatic melanoma' })
    fireEvent.change(screen.getByRole('textbox', { name: 'Your own indication' }), { target: { value: 'Stage III melanoma' } })
    fireEvent.click(screen.getByRole('button', { name: 'Check this indication' }))
    expect(ctx.dispatch).toHaveBeenCalledWith(c, 'set_indication', { text: 'Stage III melanoma' })
    fireEvent.click(screen.getByRole('button', { name: 'Let the engine choose' }))
    expect(ctx.dispatch).toHaveBeenCalledWith(c, 'set_indication', { text: null })
  })

  it('indication: says why it does not apply instead of offering choices', () => {
    show(card('indication', { applicable: false, reason: 'Only patient biosimilar studies need an indication.', ranked: [], user_indication_warnings: [] }))
    expect(screen.getByText('Only patient biosimilar studies need an indication.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Use this/ })).toBeNull()
  })

  it('guideline: choose one and confirm -> set_guideline', () => {
    const c = card('guideline', {
      selected: 'FDA-BE-2014', default: 'FDA-BE-2014',
      options: [
        { id: 'FDA-BE-2014', display_name: 'FDA Bioequivalence (2014)', regulator: 'FDA', region: 'US', citation: 'FDA 2014', is_current: true, submission_pathway: 'ANDA' },
        { id: 'EMA-BE-2010', display_name: 'EMA Bioequivalence (2010)', regulator: 'EMA', region: 'EU', citation: 'CPMP 2010', is_current: true, submission_pathway: '' },
      ],
    })
    const { ctx } = show(c)
    fireEvent.change(screen.getByRole('combobox', { name: 'Regulatory guideline' }), { target: { value: 'EMA-BE-2010' } })
    fireEvent.click(screen.getByRole('button', { name: /Use EMA Bioequivalence/ }))
    expect(ctx.dispatch).toHaveBeenCalledWith(c, 'set_guideline', { guideline_version: 'EMA-BE-2010' })
  })

  const summaryRows = [
    { path: 'drug', label: 'Drug', value: 'adalimumab', status: 'confirmed', source: 'user_click', needs_check: false },
    { path: 'population.age_min', label: 'Age', value: '18-55', status: 'default', source: 'default', needs_check: false },
    { path: 'study_type', label: 'Study type', value: 'Biosimilar PK', status: 'proposed', source: 'user_text', needs_check: true },
  ]

  it('protocol_summary: Generate -> generate_protocol {}; a proposed value is flagged', () => {
    const c = card('protocol_summary', { rows: summaryRows, ready: true, missing: [], blocking_issue: null, notes: ['Generating uses AI credits.'] }, { actions: [act('generate_protocol', 'Generate protocol')] })
    const { ctx } = show(c)
    expect(screen.getByText('Check')).toBeInTheDocument()
    expect(screen.getByText('Generating uses AI credits.')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Generate protocol' }))
    expect(ctx.dispatch).toHaveBeenCalledWith(c, 'generate_protocol', {})
  })

  it('protocol_summary: shows what is missing and the server’s blocking issue when it cannot generate', () => {
    show(card('protocol_summary', { rows: summaryRows, ready: false, missing: ['Study type', 'Design decisions'], blocking_issue: 'Biologics cannot run BE.', notes: [] }))
    expect(screen.getByText('Design decisions')).toBeInTheDocument()
    expect(screen.getByText('Biologics cannot run BE.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Generate/ })).toBeNull()
  })

  it('a server action this build has no payload for is not offered', () => {
    const c = card('notice', { level: 'info', text: 'Hello', code: null }, { actions: [act('select_drug', 'Pick'), act('not_a_real_action', 'Nope')] })
    show(c)
    expect(screen.queryByRole('button', { name: 'Pick' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Nope' })).toBeNull()
  })

  it('budget_summary: picking a protocol -> link_protocol {protocol_id}; Unlink -> null; Create -> create_budget', () => {
    const P = '33333333-3333-4333-8333-333333333333'
    const open = card('budget_summary', {
      rows: [{ path: 'drug_inn', label: 'Drug', value: 'nivolumab', status: 'confirmed', source: 'user_text', needs_check: false }],
      ready: true, missing: [], blocking_issue: null, linked_protocol: null,
      protocol_options: [{ id: P, drug_inn: 'nivolumab', study_type: 'BE', status: 'draft', version: 2, created_at: '2026-10-01T00:00:00Z' }],
      estimated_grand_total: 1234567.5, currency: 'USD', notes: [],
    }, { actions: [act('create_budget', 'Create budget')] })
    const first = show(open)
    expect(screen.getByText(/1,234,567/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('radio', { name: /nivolumab/i }))
    expect(first.ctx.dispatch).toHaveBeenCalledWith(open, 'link_protocol', { protocol_id: P })
    fireEvent.click(screen.getByRole('button', { name: 'Create budget' }))
    expect(first.ctx.dispatch).toHaveBeenCalledWith(open, 'create_budget', {})
    first.unmount()

    const linked = { ...open, data: { ...open.data, linked_protocol: (open.data.protocol_options as unknown[])[0], protocol_options: [] } }
    const second = show(linked)
    fireEvent.click(screen.getByRole('button', { name: /Unlink/ }))
    expect(second.ctx.dispatch).toHaveBeenCalledWith(linked, 'link_protocol', { protocol_id: null })
  })

  it('budget_summary: Edit details -> set_budget_fields with only what changed', () => {
    const c = card('budget_summary', {
      rows: [{ path: 'currency', label: 'Currency', value: 'USD', status: 'default', source: 'default', needs_check: false }],
      ready: true, missing: [], blocking_issue: null, linked_protocol: null, protocol_options: [], estimated_grand_total: null, currency: 'USD', notes: [],
    })
    const { ctx } = show(c)
    fireEvent.click(screen.getByRole('button', { name: 'Edit details' }))
    fireEvent.change(screen.getByLabelText('Currency'), { target: { value: 'EUR' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save details' }))
    expect(ctx.dispatch).toHaveBeenCalledWith(c, 'set_budget_fields', { patch: { currency: 'EUR' } })
  })

  it('edit_impact: shows the estimate as an estimate, warns about credits, and confirms by proposal id', () => {
    const PID = '44444444-4444-4444-8444-444444444444'
    const c = card('edit_impact', {
      proposal_id: PID, kind: 'engine', base_version: 2, editable: true, blocked_reason: null,
      changes: [{ field: 'cv_intra', label: 'Within-subject CV (%)', before: '24', after: '30' }],
      sections: [], instruction: null,
      estimate: { label: 'Quick sample-size estimate', rows: [{ label: 'Sample size', value: '40' }], note: 'Using the stored half-life.' },
      warnings: [], rerun_note: 'Changing an engine input re-runs the full generation.',
    }, { actions: [act('confirm_edit', 'Run this change'), act('discard_edit', 'Cancel', 'ghost')] })
    const { ctx } = show(c)
    expect(screen.getByText('Within-subject CV (%)')).toBeInTheDocument()
    expect(screen.getByText('Quick sample-size estimate')).toBeInTheDocument()
    expect(screen.getByText(/the final values come from the full re-run/i)).toBeInTheDocument()
    expect(screen.getByText('Re-runs generation and uses credits.')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Run this change' }))
    expect(ctx.dispatch).toHaveBeenCalledWith(c, 'confirm_edit', { proposal_id: PID })
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(ctx.dispatch).toHaveBeenCalledWith(c, 'discard_edit', { proposal_id: PID })
  })

  it('edit_diff: shows the per-section diff, changed facts and QC findings; Apply -> apply_edit', () => {
    const PID = '55555555-5555-4555-8555-555555555555'
    const c = card('edit_diff', {
      proposal_id: PID, kind: 'prose', status: 'ready', base_version: 2, stale: false, applied_version: null,
      sections: [
        { section_name: 'adverse_events', display_name: 'Adverse Events', status: 'modified', old_content: 'a', new_content: 'b', diff_lines: ['-Old sentence.', '+New sentence.'], word_count_delta: -12 },
        { section_name: 'intro', display_name: 'Introduction', status: 'unchanged', old_content: null, new_content: null, diff_lines: [], word_count_delta: 0 },
      ],
      qc: { total_checks: 3, findings: [{ title: 'Washout stated', status: 'warning', detail: 'Check the number.' }, { title: 'Fine', status: 'pass', detail: '' }] },
      facts_before: [{ label: 'Within-subject CV', value: '24%' }],
      facts_after: [{ label: 'Within-subject CV', value: '30%' }],
    }, { actions: [act('apply_edit', 'Apply to protocol'), act('discard_edit', 'Discard', 'ghost')] })
    const { ctx } = show(c)
    expect(screen.getByText('Adverse Events')).toBeInTheDocument()
    expect(screen.getByText('+New sentence.')).toBeInTheDocument()
    expect(screen.getByText('−12 words')).toBeInTheDocument()
    expect(screen.getByText('1 other section unchanged.')).toBeInTheDocument()
    expect(screen.getByText('24%')).toBeInTheDocument()
    expect(screen.getByText('30%')).toBeInTheDocument()
    expect(screen.getByText('Washout stated')).toBeInTheDocument()
    expect(screen.queryByText('Fine')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Apply to protocol' }))
    expect(ctx.dispatch).toHaveBeenCalledWith(c, 'apply_edit', { proposal_id: PID })
  })

  it('edit_diff: a stale proposal offers a re-run and no Apply', () => {
    const c = card('edit_diff', { proposal_id: '55555555-5555-4555-8555-555555555555', kind: 'engine', status: 'ready', base_version: 2, stale: true, sections: [], qc: null, facts_before: [], facts_after: [], applied_version: null }, { actions: [act('discard_edit', 'Discard', 'ghost')] })
    const { ctx } = show(c)
    expect(screen.queryByRole('button', { name: /Apply/ })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: /Re-run on the current version/ }))
    expect(ctx.sendText).toHaveBeenCalledWith(expect.stringMatching(/re-run/i))
  })

  it('protocol_ready and budget_ready link to the editor, the pricing chat and the budget', () => {
    const P = '66666666-6666-4666-8666-666666666666'
    const ready = card('protocol_ready', { protocol_id: P, drug_inn: 'adalimumab', study_type: 'biosimilar_pk', guideline_version: 'FDA-BE-2014', section_count: 17, version: 1 }, { status: 'done' })
    const first = show(ready)
    expect(screen.getByRole('link', { name: 'Open in editor' })).toHaveAttribute('href', `/app/protocols/${P}`)
    expect(screen.getByRole('link', { name: 'Price this study' })).toHaveAttribute('href', `/app/chat/new?kind=budget&protocol_id=${P}`)
    first.unmount()
    const B = '77777777-7777-4777-8777-777777777777'
    show(card('budget_ready', { budget_id: B, name: 'Nivolumab budget', drug_inn: 'nivolumab', study_type: null, currency: 'USD', grand_total: 5000000, category_count: 22, n_enrolled: 120, protocol_id: null }, { status: 'done' }))
    expect(screen.getByRole('link', { name: 'Open budget' })).toHaveAttribute('href', `/app/budgets/${B}`)
    expect(screen.getByText(/5,000,000/)).toBeInTheDocument()
  })
})

describe('polling cards', () => {
  const PID = '88888888-8888-4888-8888-888888888888'

  it('generation_progress polls the protocol, then refetches the conversation and offers the editor', async () => {
    h.getProtocol.mockResolvedValue({ id: PID, generation_status: 'complete' })
    const c = card('generation_progress', { protocol_id: PID, drug_inn: 'adalimumab', status: 'generating', error: null })
    const { ctx } = show(c)
    await waitFor(() => expect(ctx.resync).toHaveBeenCalledTimes(1))
    expect(h.getProtocol).toHaveBeenCalledWith(PID)
    expect(await screen.findByRole('link', { name: 'Open in editor' })).toHaveAttribute('href', `/app/protocols/${PID}`)
  })

  it('generation_progress does not poll a card that already finished or failed', () => {
    show(card('generation_progress', { protocol_id: PID, drug_inn: 'x', status: 'failed', error: 'The model call failed.' }, { status: 'error' }))
    expect(h.getProtocol).not.toHaveBeenCalled()
    expect(screen.getByText(/The model call failed\./)).toBeInTheDocument()
  })

  it('generation_progress reports a server failure in red and keeps the page usable', async () => {
    h.getProtocol.mockResolvedValue({ id: PID, generation_status: 'failed', generation_error: 'PubMed was unreachable.' })
    show(card('generation_progress', { protocol_id: PID, drug_inn: 'x', status: 'generating', error: null }))
    expect(await screen.findByText(/PubMed was unreachable\./)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Review and try again' })).toBeInTheDocument()
  })

  it('edit_progress polls the proposal and refetches the conversation when it is ready', async () => {
    const PROP = '99999999-9999-4999-8999-999999999999'
    h.getProposal.mockResolvedValue({ id: PROP, status: 'ready', diff: [], qc: null, facts_before: null, facts_after: null, error: null, applied_version: null })
    const c = card('edit_progress', { proposal_id: PROP, kind: 'prose', status: 'generating', error: null })
    const { ctx } = show(c)
    expect(screen.getByText(/Preparing the edit/)).toBeInTheDocument()
    await waitFor(() => expect(ctx.resync).toHaveBeenCalledTimes(1))
    expect(h.getProposal).toHaveBeenCalledWith('s1', PROP, expect.anything())
    expect(await screen.findByText(/The edit is ready/)).toBeInTheDocument()
  })
})

describe('payloadForCardAction', () => {
  it('returns undefined for an action it cannot build a payload for', () => {
    const c = card('notice', { level: 'info', text: 't', code: null })
    expect(payloadForCardAction(c, 'select_drug')).toBeUndefined()
    expect(payloadForCardAction(c, 'bogus')).toBeUndefined()
    expect(payloadForCardAction(c, 'generate_protocol')).toEqual({})
  })

  it('reads the proposal id from the card for edit actions', () => {
    const c = card('edit_impact', { proposal_id: 'abc' })
    expect(payloadForCardAction(c, 'confirm_edit')).toEqual({ proposal_id: 'abc' })
    expect(payloadForCardAction(card('edit_impact', {}), 'confirm_edit')).toBeUndefined()
  })
})

// Keep the `within` import honest for future queries.
void within
