import { useEffect, useId, useState } from 'react'
import type { ReactNode } from 'react'
import { api } from '../api/client'
import { Badge, Banner, Card, Field, Input, Select, Spinner } from '../ui'
import type { Budget, BudgetGeographyProfile, BudgetUpdatePayload, StudyType } from '../types'

interface Props {
  budget: Budget
  /** A budget-level PATCH returns the WHOLE budget (rollup included) —
   *  the editor just replaces its state with it. */
  onBudgetUpdated: (b: Budget) => void
  onError: (msg: string) => void
}

const STUDY_TYPES: StudyType[] = ['BE', 'biosimilar_pk', 'food_effect']
const STUDY_LABELS: Record<StudyType, string> = {
  BE: 'Bioequivalence',
  biosimilar_pk: 'Biosimilar PK',
  food_effect: 'Food Effect',
}

const GEOGRAPHY_LABELS: Record<BudgetGeographyProfile, string> = {
  us_only: 'United States only',
  eu_west: 'Western Europe',
  eu_east: 'Eastern Europe',
  latam: 'Latin America',
  apac: 'Asia-Pacific',
  china: 'China',
  india: 'India',
  global_emerging_mixed: 'Global — emerging markets mix',
  global_developed_mixed: 'Global — developed markets mix',
}
const GEOGRAPHY_OPTIONS: BudgetGeographyProfile[] = [
  'global_emerging_mixed', 'global_developed_mixed', 'us_only', 'eu_west', 'eu_east', 'latam', 'apac', 'china', 'india',
]

const THERAPEUTIC_AREA_OPTIONS: { value: 'oncology' | 'other' | 'unknown'; label: string }[] = [
  { value: 'oncology', label: 'Oncology' },
  { value: 'other', label: 'Other' },
]

function FromProtocolPill() {
  return <Badge variant="info">From protocol</Badge>
}
function EditedPill() {
  return <Badge variant="warning">Edited</Badge>
}
function AutoEstimatedPill() {
  return <Badge>Estimated</Badge>
}
/** `inferred_from_safety_profile` — the value came from the drug's curated
 *  safety knowledge base (e.g. "not NHV-safe"), not a plain estimate. */
function FromDrugProfilePill() {
  return <Badge variant="info">From drug profile</Badge>
}
/** `phase_scaled` — derived by scaling a per-month/per-visit rate across the
 *  study's own phase durations, distinct from a flat rate-card estimate. */
function ScaledPill() {
  return <Badge variant="warning">Scaled</Badge>
}

/** Which pill (if any) a field gets, per the assumption record rather than
 *  the blanket `linked` flag. `title` on the label surfaces the server's own
 *  `basis` string. */
function fieldPill(budget: Budget, key: string): { pill: ReactNode; title?: string } {
  const a = budget.derivation?.assumptions?.[key]
  if (!a) return { pill: null }
  if (a.source === 'protocol') return { pill: <FromProtocolPill />, title: a.basis }
  if (a.source === 'user') return { pill: null, title: a.basis }
  if (a.source === 'inferred_from_safety_profile') return { pill: <FromDrugProfilePill />, title: a.basis }
  if (a.source === 'phase_scaled') return { pill: <ScaledPill />, title: a.basis }
  return { pill: <AutoEstimatedPill />, title: a.basis }
}

function numOrNull(raw: string): number | null {
  const t = raw.trim()
  if (t === '') return null
  const n = Number(t)
  return Number.isFinite(n) ? n : null
}

export default function BudgetSettingsCard({ budget, onBudgetUpdated, onError }: Props) {
  const linked = budget.protocol_id !== null
  const isPatient = budget.subject_profile === 'patient'

  // Fields the user has changed via this form since the last (re-)link.
  // There's no stored "originally derived" value for drug_inn/study_type to
  // diff against on every render (only n_enrolled has one, in
  // budget.derivation.n_enrolled) — so this session-local set is how those
  // two fields pick up the "Edited" pill. It resets whenever a fresh link
  // happens (derived_at changes) or the protocol is unlinked entirely.
  const [editedFields, setEditedFields] = useState<Set<string>>(new Set())
  useEffect(() => { setEditedFields(new Set()) }, [budget.derivation?.derived_at, budget.protocol_id])

  const nEnrolledEdited =
    linked && budget.derivation?.n_enrolled != null && budget.n_enrolled !== budget.derivation.n_enrolled

  const [drugInn, setDrugInn] = useState(budget.drug_inn ?? '')
  const [nEnrolled, setNEnrolled] = useState(budget.n_enrolled != null ? String(budget.n_enrolled) : '')
  const [nSites, setNSites] = useState(budget.n_sites != null ? String(budget.n_sites) : '')
  const [nDosingGroups, setNDosingGroups] = useState(budget.n_dosing_groups != null ? String(budget.n_dosing_groups) : '')
  const [screenFailPct, setScreenFailPct] = useState(budget.screen_failure_rate_pct != null ? String(budget.screen_failure_rate_pct) : '')
  const [nScreenFailures, setNScreenFailures] = useState(budget.n_screen_failures != null ? String(budget.n_screen_failures) : '')
  const [nAlternates, setNAlternates] = useState(budget.n_alternates != null ? String(budget.n_alternates) : '')
  const [nScreened, setNScreened] = useState(budget.n_screened != null ? String(budget.n_screened) : '')
  const [projectMonths, setProjectMonths] = useState(budget.project_duration_months != null ? String(budget.project_duration_months) : '')

  // ── Patient-only drivers (§6.3) ─────────────────────────────────────────
  const [indication, setIndication] = useState(budget.indication ?? '')
  const [therapeuticArea, setTherapeuticArea] = useState(budget.therapeutic_area ?? '')
  const [dropoutRatePct, setDropoutRatePct] = useState(budget.dropout_rate_pct != null ? String(budget.dropout_rate_pct) : '')
  const [nVisitsPerPatient, setNVisitsPerPatient] = useState(budget.n_visits_per_patient != null ? String(budget.n_visits_per_patient) : '')
  const [nSitesSelected, setNSitesSelected] = useState(budget.n_sites_selected != null ? String(budget.n_sites_selected) : '')
  const [nCountries, setNCountries] = useState(budget.n_countries != null ? String(budget.n_countries) : '')
  const [geographyProfile, setGeographyProfile] = useState<BudgetGeographyProfile | ''>(budget.geography_profile ?? '')
  const [startupMonths, setStartupMonths] = useState(budget.startup_months != null ? String(budget.startup_months) : '')
  const [recruitmentMonths, setRecruitmentMonths] = useState(budget.recruitment_months != null ? String(budget.recruitment_months) : '')
  const [treatmentMonths, setTreatmentMonths] = useState(budget.treatment_months != null ? String(budget.treatment_months) : '')
  const [followupMonths, setFollowupMonths] = useState(budget.followup_months != null ? String(budget.followup_months) : '')
  const [closeoutMonths, setCloseoutMonths] = useState(budget.closeout_months != null ? String(budget.closeout_months) : '')
  const [nExpectedSaes, setNExpectedSaes] = useState(budget.n_expected_saes != null ? String(budget.n_expected_saes) : '')
  const [nExpectedSusars, setNExpectedSusars] = useState(budget.n_expected_susars != null ? String(budget.n_expected_susars) : '')
  const [nMonitoringOnsite, setNMonitoringOnsite] = useState(budget.n_monitoring_visits_onsite != null ? String(budget.n_monitoring_visits_onsite) : '')
  const [nMonitoringRemote, setNMonitoringRemote] = useState(budget.n_monitoring_visits_remote != null ? String(budget.n_monitoring_visits_remote) : '')
  const [nPharmacyVisits, setNPharmacyVisits] = useState(budget.n_pharmacy_visits != null ? String(budget.n_pharmacy_visits) : '')
  const [nProtocolAmendments, setNProtocolAmendments] = useState(budget.n_protocol_amendments != null ? String(budget.n_protocol_amendments) : '')
  const [archivalRetentionYears, setArchivalRetentionYears] = useState(budget.archival_retention_years != null ? String(budget.archival_retention_years) : '')
  const [reprising, setReprising] = useState(false)

  function revertDrafts() {
    setDrugInn(budget.drug_inn ?? '')
    setNEnrolled(budget.n_enrolled != null ? String(budget.n_enrolled) : '')
    setNSites(budget.n_sites != null ? String(budget.n_sites) : '')
    setNDosingGroups(budget.n_dosing_groups != null ? String(budget.n_dosing_groups) : '')
    setScreenFailPct(budget.screen_failure_rate_pct != null ? String(budget.screen_failure_rate_pct) : '')
    setNScreenFailures(budget.n_screen_failures != null ? String(budget.n_screen_failures) : '')
    setNAlternates(budget.n_alternates != null ? String(budget.n_alternates) : '')
    setNScreened(budget.n_screened != null ? String(budget.n_screened) : '')
    setProjectMonths(budget.project_duration_months != null ? String(budget.project_duration_months) : '')
    setIndication(budget.indication ?? '')
    setTherapeuticArea(budget.therapeutic_area ?? '')
    setDropoutRatePct(budget.dropout_rate_pct != null ? String(budget.dropout_rate_pct) : '')
    setNVisitsPerPatient(budget.n_visits_per_patient != null ? String(budget.n_visits_per_patient) : '')
    setNSitesSelected(budget.n_sites_selected != null ? String(budget.n_sites_selected) : '')
    setNCountries(budget.n_countries != null ? String(budget.n_countries) : '')
    setGeographyProfile(budget.geography_profile ?? '')
    setStartupMonths(budget.startup_months != null ? String(budget.startup_months) : '')
    setRecruitmentMonths(budget.recruitment_months != null ? String(budget.recruitment_months) : '')
    setTreatmentMonths(budget.treatment_months != null ? String(budget.treatment_months) : '')
    setFollowupMonths(budget.followup_months != null ? String(budget.followup_months) : '')
    setCloseoutMonths(budget.closeout_months != null ? String(budget.closeout_months) : '')
    setNExpectedSaes(budget.n_expected_saes != null ? String(budget.n_expected_saes) : '')
    setNExpectedSusars(budget.n_expected_susars != null ? String(budget.n_expected_susars) : '')
    setNMonitoringOnsite(budget.n_monitoring_visits_onsite != null ? String(budget.n_monitoring_visits_onsite) : '')
    setNMonitoringRemote(budget.n_monitoring_visits_remote != null ? String(budget.n_monitoring_visits_remote) : '')
    setNPharmacyVisits(budget.n_pharmacy_visits != null ? String(budget.n_pharmacy_visits) : '')
    setNProtocolAmendments(budget.n_protocol_amendments != null ? String(budget.n_protocol_amendments) : '')
    setArchivalRetentionYears(budget.archival_retention_years != null ? String(budget.archival_retention_years) : '')
  }

  async function patch(field: string, payload: BudgetUpdatePayload) {
    try {
      const updated = await api.updateBudget(budget.id, payload)
      onBudgetUpdated(updated)
      if (linked && (field === 'drug_inn' || field === 'study_type')) {
        setEditedFields(prev => new Set(prev).add(field))
      }
      return updated
    } catch (e) {
      onError(e instanceof Error ? e.message : 'Failed to save budget settings')
      revertDrafts()
      return null
    }
  }

  /** n_sites and the five phase durations drive ~70% of a patient budget's
   *  value, and a plain PATCH never moves a total (see api/client.ts —
   *  updateBudget vs repriceBudget). So every footprint/timeline field on a
   *  patient budget re-prices immediately after it saves, rather than
   *  leaving the numbers stale until the user notices and goes looking for a
   *  refresh button — the documented "stored engine results are stale"
   *  failure mode this codebase has already been bitten by once. */
  async function patchAndReprice(field: string, payload: BudgetUpdatePayload) {
    const updated = await patch(field, payload)
    if (!updated || !isPatient) return
    setReprising(true)
    try {
      const repriced = await api.repriceBudget(budget.id)
      onBudgetUpdated(repriced)
    } catch (e) {
      onError(e instanceof Error ? e.message : 'Saved, but failed to re-price the line items — use Refresh from protocol or edit rows directly.')
    } finally {
      setReprising(false)
    }
  }

  const drugInnPill = fieldPill(budget, 'drug_inn')
  const studyTypePill = fieldPill(budget, 'study_type')
  const nEnrolledPill = fieldPill(budget, 'n_enrolled')
  const nSitesPill = fieldPill(budget, 'n_sites')
  const nDosingGroupsPill = fieldPill(budget, 'n_dosing_groups')
  const projectMonthsPill = fieldPill(budget, 'project_duration_months')
  const screenFailPctPill = fieldPill(budget, 'screen_failure_rate_pct')
  const nScreenFailuresPill = fieldPill(budget, 'n_screen_failures')
  const nAlternatesPill = fieldPill(budget, 'n_alternates')
  const nScreenedPill = fieldPill(budget, 'n_screened')

  const indicationPill = fieldPill(budget, 'indication')
  const therapeuticAreaPill = fieldPill(budget, 'therapeutic_area')
  const dropoutPill = fieldPill(budget, 'dropout_rate_pct')
  const visitsPerPatientPill = fieldPill(budget, 'n_visits_per_patient')
  const nSitesSelectedPill = fieldPill(budget, 'n_sites_selected')
  const nCountriesPill = fieldPill(budget, 'n_countries')
  const geographyPill = fieldPill(budget, 'geography_profile')
  const saesPill = fieldPill(budget, 'n_expected_saes')
  const susarsPill = fieldPill(budget, 'n_expected_susars')
  const monitoringOnsitePill = fieldPill(budget, 'n_monitoring_visits_onsite')
  const monitoringRemotePill = fieldPill(budget, 'n_monitoring_visits_remote')
  const pharmacyVisitsPill = fieldPill(budget, 'n_pharmacy_visits')
  const amendmentsPill = fieldPill(budget, 'n_protocol_amendments')
  const archivalPill = fieldPill(budget, 'archival_retention_years')

  const totalMonths = [startupMonths, recruitmentMonths, treatmentMonths, followupMonths, closeoutMonths]
    .map(v => Number(v))
    .reduce((sum, n) => sum + (Number.isFinite(n) ? n : 0), 0)

  const nCompleted = budget.n_completed ?? (
    budget.n_enrolled != null && budget.dropout_rate_pct != null
      ? Math.round(budget.n_enrolled * (1 - budget.dropout_rate_pct / 100))
      : null
  )

  const comparatorBasis = budget.derivation?.assumptions?.['comparator_sourced_by_sponsor']?.basis

  return (
    <Card padding="lg" className="space-y-6">
      {/* Auto-estimated on create; every one of these is editable. */}
      <div className="space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h3 className="text-h3 text-fg">Study details</h3>
            <p className="mt-0.5 text-small text-fg-subtle">
              Auto-estimated from the drug and study type — edit any of them.
            </p>
          </div>
          {reprising && (
            <span role="status" className="flex items-center gap-1.5 text-small font-medium text-[color:var(--link)]">
              <Spinner size={14} />
              Re-pricing…
            </span>
          )}
        </div>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
          <Field
            label={
              <PillLabel title={drugInnPill.title} text="Drug (INN)">
                {linked && <FromProtocolPill />}
                {!linked && drugInnPill.pill}
                {editedFields.has('drug_inn') && <EditedPill />}
              </PillLabel>
            }
          >
            <Input
              value={drugInn}
              onChange={e => setDrugInn(e.target.value)}
              onBlur={() => {
                if (drugInn.trim() !== (budget.drug_inn ?? '')) patch('drug_inn', { drug_inn: drugInn.trim() || null })
              }}
              placeholder="—"
            />
          </Field>
          <Field
            label={
              <PillLabel title={studyTypePill.title} text="Study type">
                {linked && <FromProtocolPill />}
                {!linked && studyTypePill.pill}
                {editedFields.has('study_type') && <EditedPill />}
              </PillLabel>
            }
          >
            <Select
              value={budget.study_type ?? ''}
              onChange={e => patch('study_type', { study_type: (e.target.value || null) as StudyType | null })}
            >
              <option value="">—</option>
              {STUDY_TYPES.map(s => <option key={s} value={s}>{STUDY_LABELS[s]}</option>)}
            </Select>
          </Field>
          <Field
            label={
              <PillLabel title={nEnrolledPill.title} text={isPatient ? 'Patients enrolled' : 'Subjects enrolled'}>
                {linked && <FromProtocolPill />}
                {!linked && nEnrolledPill.pill}
                {nEnrolledEdited && <EditedPill />}
              </PillLabel>
            }
          >
            <Input
              value={nEnrolled}
              onChange={e => setNEnrolled(e.target.value)}
              onBlur={() => {
                const n = numOrNull(nEnrolled)
                if (n !== budget.n_enrolled) patchAndReprice('n_enrolled', { n_enrolled: n })
              }}
              placeholder="—"
              className="tabular-nums"
            />
          </Field>
        </div>
      </div>

      {/* Study parameters — also auto-estimated, but re-linking a protocol
       *  never re-derives these from it: adjust the affected rows directly,
       *  or re-link to re-derive everything. */}
      <div className="space-y-3 border-t border-line pt-5">
        <div>
          <h3 className="text-h3 text-fg">Study parameters</h3>
          <p className="mt-0.5 text-small text-fg-subtle">
            {isPatient
              ? 'Sites and dosing groups here are estimated too — see Footprint below for the patient-study drivers, which DO re-price automatically.'
              : 'Estimated too. Changing one here doesn’t re-price the line items below — adjust the affected rows directly, or re-link a protocol to re-derive everything.'}
          </p>
        </div>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
          <NumberField
            label="Sites"
            pill={nSitesPill.pill}
            title={nSitesPill.title}
            value={nSites}
            onChange={setNSites}
            onBlur={() => { const n = numOrNull(nSites); if (n !== budget.n_sites) patch('n_sites', { n_sites: n }) }}
          />
          <NumberField
            label="Dosing groups"
            pill={nDosingGroupsPill.pill}
            title={nDosingGroupsPill.title}
            value={nDosingGroups}
            onChange={setNDosingGroups}
            onBlur={() => { const n = numOrNull(nDosingGroups); if (n !== budget.n_dosing_groups) patch('n_dosing_groups', { n_dosing_groups: n }) }}
          />
          <NumberField
            label="Project duration (months)"
            pill={projectMonthsPill.pill}
            title={projectMonthsPill.title}
            value={projectMonths}
            onChange={setProjectMonths}
            onBlur={() => { const n = numOrNull(projectMonths); if (n !== budget.project_duration_months) patch('project_duration_months', { project_duration_months: n }) }}
          />
          <NumberField
            label="Screen failure rate (%)"
            pill={screenFailPctPill.pill}
            title={screenFailPctPill.title}
            value={screenFailPct}
            onChange={setScreenFailPct}
            onBlur={() => { const n = numOrNull(screenFailPct); if (n !== budget.screen_failure_rate_pct) patch('screen_failure_rate_pct', { screen_failure_rate_pct: n }) }}
          />
          <NumberField
            label="Screen failures"
            pill={nScreenFailuresPill.pill}
            title={nScreenFailuresPill.title}
            value={nScreenFailures}
            onChange={setNScreenFailures}
            onBlur={() => { const n = numOrNull(nScreenFailures); if (n !== budget.n_screen_failures) patch('n_screen_failures', { n_screen_failures: n }) }}
          />
          {/* A patient budget forces n_alternates to 0 server-side — hidden
           *  entirely rather than shown disabled. */}
          {!isPatient && (
            <NumberField
              label="Alternates"
              pill={nAlternatesPill.pill}
              title={nAlternatesPill.title}
              value={nAlternates}
              onChange={setNAlternates}
              onBlur={() => { const n = numOrNull(nAlternates); if (n !== budget.n_alternates) patch('n_alternates', { n_alternates: n }) }}
            />
          )}
          <NumberField
            label="Screened"
            pill={nScreenedPill.pill}
            title={nScreenedPill.title}
            value={nScreened}
            onChange={v => setNScreened(v)}
            onBlur={() => {
              const n = numOrNull(nScreened)
              if (n !== budget.n_screened) patch('n_screened', { n_screened: n })
            }}
          />
        </div>
      </div>

      {isPatient && (
        <>
          {/* 1. Population */}
          <div className="space-y-3 border-t border-line pt-5">
            <h3 className="text-h3 text-fg">Population</h3>
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
              <div className="sm:col-span-3">
                <Field
                  label={
                    <PillLabel title={indicationPill.title} text="Indication">
                      {indicationPill.pill}
                    </PillLabel>
                  }
                >
                  <Input
                    value={indication}
                    onChange={e => setIndication(e.target.value)}
                    onBlur={() => { if (indication.trim() !== (budget.indication ?? '')) patch('indication', { indication: indication.trim() || null }) }}
                    placeholder="e.g. unresectable or metastatic melanoma"
                  />
                </Field>
              </div>
              <Field
                label={
                  <PillLabel title={therapeuticAreaPill.title} text="Therapeutic area">
                    {therapeuticAreaPill.pill}
                  </PillLabel>
                }
              >
                <Select
                  value={therapeuticArea}
                  onChange={e => { setTherapeuticArea(e.target.value); patch('therapeutic_area', { therapeutic_area: (e.target.value || null) as 'oncology' | 'other' | null }) }}
                >
                  <option value="">—</option>
                  {THERAPEUTIC_AREA_OPTIONS.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
                </Select>
              </Field>
              <NumberField
                label="Screen failure rate (%)"
                pill={screenFailPctPill.pill}
                title={screenFailPctPill.title}
                value={screenFailPct}
                onChange={setScreenFailPct}
                onBlur={() => { const n = numOrNull(screenFailPct); if (n !== budget.screen_failure_rate_pct) patch('screen_failure_rate_pct', { screen_failure_rate_pct: n }) }}
              />
              <NumberField
                label="Drop-out rate (%)"
                pill={dropoutPill.pill}
                title={dropoutPill.title}
                value={dropoutRatePct}
                onChange={setDropoutRatePct}
                onBlur={() => { const n = numOrNull(dropoutRatePct); if (n !== budget.dropout_rate_pct) patchAndReprice('dropout_rate_pct', { dropout_rate_pct: n }) }}
              />
              <Field
                label={
                  <PillLabel title="Server-computed: enrolled minus expected drop-outs." text="Patients completing" />
                }
              >
                <Input
                  readOnly
                  value={nCompleted ?? '—'}
                  className="cursor-default bg-surface-muted tabular-nums text-fg-muted"
                />
              </Field>
              <NumberField
                label="Visits per patient"
                pill={visitsPerPatientPill.pill}
                title={visitsPerPatientPill.title}
                value={nVisitsPerPatient}
                onChange={setNVisitsPerPatient}
                onBlur={() => { const n = numOrNull(nVisitsPerPatient); if (n !== budget.n_visits_per_patient) patchAndReprice('n_visits_per_patient', { n_visits_per_patient: n }) }}
              />
            </div>
          </div>

          {/* 2. Footprint */}
          <div className="space-y-3 border-t border-line pt-5">
            <h3 className="text-h3 text-fg">Footprint</h3>
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
              <NumberField
                label="Sites activated"
                pill={nSitesSelectedPill.pill}
                title={nSitesSelectedPill.title}
                value={nSitesSelected}
                onChange={setNSitesSelected}
                onBlur={() => { const n = numOrNull(nSitesSelected); if (n !== budget.n_sites_selected) patchAndReprice('n_sites_selected', { n_sites_selected: n }) }}
              />
              <NumberField
                label="Sites contracted"
                pill={nSitesPill.pill}
                title={nSitesPill.title}
                value={nSites}
                onChange={setNSites}
                onBlur={() => { const n = numOrNull(nSites); if (n !== budget.n_sites) patchAndReprice('n_sites', { n_sites: n }) }}
              />
              <NumberField
                label="Countries"
                pill={nCountriesPill.pill}
                title={nCountriesPill.title}
                value={nCountries}
                onChange={setNCountries}
                onBlur={() => { const n = numOrNull(nCountries); if (n !== budget.n_countries) patchAndReprice('n_countries', { n_countries: n }) }}
              />
              <Field
                label={
                  <PillLabel title={geographyPill.title} text="Geography">
                    {geographyPill.pill}
                  </PillLabel>
                }
              >
                <Select
                  value={geographyProfile}
                  onChange={e => {
                    const v = e.target.value as BudgetGeographyProfile | ''
                    setGeographyProfile(v)
                    patchAndReprice('geography_profile', { geography_profile: (v || null) as BudgetGeographyProfile | null })
                  }}
                >
                  <option value="">—</option>
                  {GEOGRAPHY_OPTIONS.map(g => <option key={g} value={g}>{GEOGRAPHY_LABELS[g]}</option>)}
                </Select>
              </Field>
              <Field
                label={
                  <PillLabel
                    title="The multiplier applied to US-baseline unit costs on every geography-scaled line, reflecting typically-lower ex-US site and staffing costs."
                    text="Geography factor"
                  />
                }
              >
                <Input
                  readOnly
                  value={budget.geography_factor != null ? budget.geography_factor.toFixed(2) : '—'}
                  className="cursor-default bg-surface-muted tabular-nums text-fg-muted"
                />
              </Field>
            </div>
          </div>

          {/* 3. Timeline */}
          <div className="space-y-3 border-t border-line pt-5">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <h3 className="text-h3 text-fg">Timeline</h3>
              <span className="text-body font-semibold tabular-nums text-fg">
                Total {totalMonths.toFixed(1)} months
              </span>
            </div>
            <p className="text-small text-fg-subtle">
              Nearly every cost in a patient budget is priced per month of a specific phase. Editing one phase
              re-prices only the lines that use it.
            </p>
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
              <NumberField
                label="Start-up"
                value={startupMonths}
                onChange={setStartupMonths}
                onBlur={() => { const n = numOrNull(startupMonths); if (n !== budget.startup_months) patchAndReprice('startup_months', { startup_months: n }) }}
              />
              <NumberField
                label="Recruitment"
                value={recruitmentMonths}
                onChange={setRecruitmentMonths}
                onBlur={() => { const n = numOrNull(recruitmentMonths); if (n !== budget.recruitment_months) patchAndReprice('recruitment_months', { recruitment_months: n }) }}
              />
              <NumberField
                label="Treatment"
                value={treatmentMonths}
                onChange={setTreatmentMonths}
                onBlur={() => { const n = numOrNull(treatmentMonths); if (n !== budget.treatment_months) patchAndReprice('treatment_months', { treatment_months: n }) }}
              />
              <NumberField
                label="Follow-up"
                value={followupMonths}
                onChange={setFollowupMonths}
                onBlur={() => { const n = numOrNull(followupMonths); if (n !== budget.followup_months) patchAndReprice('followup_months', { followup_months: n }) }}
              />
              <NumberField
                label="Close-out"
                value={closeoutMonths}
                onChange={setCloseoutMonths}
                onBlur={() => { const n = numOrNull(closeoutMonths); if (n !== budget.closeout_months) patchAndReprice('closeout_months', { closeout_months: n }) }}
              />
            </div>
          </div>

          {/* 4. Volume */}
          <div className="space-y-3 border-t border-line pt-5">
            <h3 className="text-h3 text-fg">Volume</h3>
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
              <NumberField
                label="Expected SAEs"
                pill={saesPill.pill}
                title={saesPill.title}
                value={nExpectedSaes}
                onChange={setNExpectedSaes}
                onBlur={() => { const n = numOrNull(nExpectedSaes); if (n !== budget.n_expected_saes) patchAndReprice('n_expected_saes', { n_expected_saes: n }) }}
              />
              <NumberField
                label="Expected SUSARs"
                pill={susarsPill.pill}
                title={susarsPill.title}
                value={nExpectedSusars}
                onChange={setNExpectedSusars}
                onBlur={() => { const n = numOrNull(nExpectedSusars); if (n !== budget.n_expected_susars) patchAndReprice('n_expected_susars', { n_expected_susars: n }) }}
              />
              <NumberField
                label="Monitoring visits (on-site)"
                pill={monitoringOnsitePill.pill}
                title={monitoringOnsitePill.title}
                value={nMonitoringOnsite}
                onChange={setNMonitoringOnsite}
                onBlur={() => { const n = numOrNull(nMonitoringOnsite); if (n !== budget.n_monitoring_visits_onsite) patchAndReprice('n_monitoring_visits_onsite', { n_monitoring_visits_onsite: n }) }}
              />
              <NumberField
                label="Monitoring visits (remote)"
                pill={monitoringRemotePill.pill}
                title={monitoringRemotePill.title}
                value={nMonitoringRemote}
                onChange={setNMonitoringRemote}
                onBlur={() => { const n = numOrNull(nMonitoringRemote); if (n !== budget.n_monitoring_visits_remote) patchAndReprice('n_monitoring_visits_remote', { n_monitoring_visits_remote: n }) }}
              />
              <NumberField
                label="Pharmacy visits"
                pill={pharmacyVisitsPill.pill}
                title={pharmacyVisitsPill.title}
                value={nPharmacyVisits}
                onChange={setNPharmacyVisits}
                onBlur={() => { const n = numOrNull(nPharmacyVisits); if (n !== budget.n_pharmacy_visits) patchAndReprice('n_pharmacy_visits', { n_pharmacy_visits: n }) }}
              />
              <NumberField
                label="Protocol amendments"
                pill={amendmentsPill.pill}
                title={amendmentsPill.title}
                value={nProtocolAmendments}
                onChange={setNProtocolAmendments}
                onBlur={() => { const n = numOrNull(nProtocolAmendments); if (n !== budget.n_protocol_amendments) patchAndReprice('n_protocol_amendments', { n_protocol_amendments: n }) }}
              />
              <NumberField
                label="Archival retention (years)"
                pill={archivalPill.pill}
                title={archivalPill.title}
                value={archivalRetentionYears}
                onChange={setArchivalRetentionYears}
                onBlur={() => { const n = numOrNull(archivalRetentionYears); if (n !== budget.archival_retention_years) patchAndReprice('archival_retention_years', { archival_retention_years: n }) }}
              />
            </div>
          </div>

          {/* 5. Options */}
          <div className="space-y-3 border-t border-line pt-5">
            <h3 className="text-h3 text-fg">Options</h3>
            <div className="flex flex-col gap-4">
              <Toggle
                label="BICR required"
                sublabel="Blinded independent central review of imaging."
                checked={!!budget.bicr_required}
                onChange={v => patchAndReprice('bicr_required', { bicr_required: v })}
              />
              <Toggle
                label="Comparator sourced by sponsor"
                sublabel="Off: the site/patient sources the comparator. On: your organisation purchases and supplies it."
                checked={!!budget.comparator_sourced_by_sponsor}
                onChange={v => patchAndReprice('comparator_sourced_by_sponsor', { comparator_sourced_by_sponsor: v })}
              />
              {budget.comparator_sourced_by_sponsor && (
                <Banner tone="warning">
                  {comparatorBasis ??
                    'Switching this on prices a full purchased comparator arm at current list price. Only switch it on if your organisation is buying the reference product.'}
                </Banner>
              )}
            </div>
          </div>
        </>
      )}
    </Card>
  )
}

/** A field label that carries its provenance pill(s) and the server's own
 *  `basis` string as a tooltip. */
function PillLabel({ text, title, children }: { text: string; title?: string; children?: ReactNode }) {
  return (
    <span title={title} className="inline-flex flex-wrap items-center gap-1.5">
      {text}
      {children}
    </span>
  )
}

/** A labelled numeric text input (stored as a string draft, parsed on blur). */
function NumberField({ label, pill, title, value, onChange, onBlur }: {
  label: string
  pill?: ReactNode
  title?: string
  value: string
  onChange: (v: string) => void
  onBlur: () => void
}) {
  return (
    <Field label={<PillLabel title={title} text={label}>{pill}</PillLabel>}>
      <Input
        value={value}
        onChange={e => onChange(e.target.value)}
        onBlur={onBlur}
        placeholder="—"
        className="tabular-nums"
      />
    </Field>
  )
}

/** On/off switch. A real <button role="switch"> so it is focusable and
 *  Space/Enter toggle it (the old markup was a click-only span). */
function Toggle({ label, sublabel, checked, onChange }: {
  label: string
  sublabel?: string
  checked: boolean
  onChange: (v: boolean) => void
}) {
  const id = useId()
  return (
    <div className="flex items-start justify-between gap-4">
      <span>
        <span id={`${id}-label`} className="block text-small font-medium text-fg">{label}</span>
        {sublabel && (
          <span id={`${id}-desc`} className="mt-0.5 block max-w-md text-small text-fg-subtle">{sublabel}</span>
        )}
      </span>
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        aria-labelledby={`${id}-label`}
        aria-describedby={sublabel ? `${id}-desc` : undefined}
        onClick={() => onChange(!checked)}
        className={`relative h-6 w-10 flex-none cursor-pointer rounded-full transition-colors duration-fast ${
          checked ? 'bg-[var(--btn-primary-bg)]' : 'bg-line-strong'
        }`}
      >
        <span
          aria-hidden
          className={`absolute left-0.5 top-0.5 h-5 w-5 rounded-full bg-white shadow-xs transition-transform duration-fast ${
            checked ? 'translate-x-4' : ''
          }`}
        />
      </button>
    </div>
  )
}
