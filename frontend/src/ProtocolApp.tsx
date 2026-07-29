import { useState } from 'react'
import { Link } from 'react-router-dom'
import Stepper from './components/Stepper'
import DrugSelector from './components/DrugSelector'
import StudyTypeSelector, { allowedStudyTypes } from './components/StudyTypeSelector'
import PopulationBuilder from './components/PopulationBuilder'
import GenerateButton from './components/GenerateButton'
import ReportsView from './components/ReportsView'
import CSRView from './components/CSRView'
import ReviewDesign from './components/ReviewDesign'
import TeamView from './components/TeamView'
import ProtocolEditor from './editor/ProtocolEditor'
import { AccountButton } from './lib/auth'
import { ActingUserPicker } from './lib/actingUser'
import { api } from './api/client'
import { defaultPopulationConfig } from './types'
import type { DesignDecision, Drug, GeneratedProtocol, PopulationConfig, ProtocolSummary, StudyType } from './types'

const STEPS = ['Select Drug', 'Study Type', 'Population', 'Review Design', 'Generate']

const STEP_META = [
  {
    icon: (
      <svg width="20" height="20" viewBox="0 0 20 20" fill="none">
        <rect x="2" y="8" width="16" height="4.5" rx="2.25" stroke="currentColor" strokeWidth="1.5" />
        <path d="M8 10.25h4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
        <path d="M10 8V6M10 12.5V14.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
      </svg>
    ),
    title: 'Select Investigational Drug',
    subtitle:
      'Search by International Nonproprietary Name (INN). The drug drives PubMed literature fetching and PK parameter extraction.',
  },
  {
    icon: (
      <svg width="20" height="20" viewBox="0 0 20 20" fill="none">
        <path d="M10 2l2.5 5.5H18l-4.5 3.5 1.5 5.5L10 13l-5 3.5 1.5-5.5L2 7.5h5.5L10 2z" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
      </svg>
    ),
    title: 'Select Study Type',
    subtitle:
      'Choose the regulatory pathway that matches your study objectives. Each type maps to a specific FDA guidance document.',
  },
  {
    icon: (
      <svg width="20" height="20" viewBox="0 0 20 20" fill="none">
        <circle cx="10" cy="7" r="3" stroke="currentColor" strokeWidth="1.5" />
        <path d="M3 18c0-3.87 3.13-7 7-7s7 3.13 7 7" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
      </svg>
    ),
    title: 'Define Subject Population',
    subtitle:
      'Configure eligibility criteria. These settings inform the Subject Selection, Dosing, and Safety Monitoring sections.',
  },
  {
    icon: (
      <svg width="20" height="20" viewBox="0 0 20 20" fill="none">
        <path d="M7 4l-4 4 4 4M13 4l4 4-4 4M4 8h13" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    ),
    title: 'Review Study Design',
    subtitle:
      'Population, dose, route, design, and PD endpoint — determined from the product’s safety profile and PK. Confirm or change each before generating.',
  },
  {
    icon: (
      <svg width="20" height="20" viewBox="0 0 20 20" fill="none">
        <path d="M4 3h9l4 4v11a1 1 0 01-1 1H4a1 1 0 01-1-1V4a1 1 0 011-1z" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
        <path d="M13 3v5h4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
        <path d="M7 10h6M7 13h4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
      </svg>
    ),
    title: 'Review & Generate Protocol',
    subtitle:
      'Confirm all settings before generating. The platform fetches literature, extracts PK parameters, and generates all FDA-guideline-mapped sections using Claude.',
  },
]

type AppView = 'reports' | 'wizard' | 'team'

function Header({ view, onNavReports, onNavNew, onNavTeam }: {
  view: AppView | 'editor'
  onNavReports: () => void
  onNavNew: () => void
  onNavTeam: () => void
}) {
  const navBtn = (active: boolean) => [
    'text-xs font-semibold px-3 py-1.5 rounded-lg transition-colors cursor-pointer',
    active
      ? 'bg-cyan-500/15 text-cyan-300 border border-cyan-500/25'
      : 'text-slate-400 hover:text-white hover:bg-white/8 border border-transparent',
  ].join(' ')

  return (
    <header style={{ background: 'var(--navy)' }} className="border-b border-white/5 sticky top-0 z-30">
      <div className="max-w-5xl mx-auto px-6 py-3 flex items-center justify-between gap-4">
        <Link to="/" className="flex items-center gap-3 group flex-shrink-0" title="Back to home">
          <div className="w-9 h-9 rounded-xl bg-cyan-500/15 border border-cyan-400/25 flex items-center justify-center flex-shrink-0 transition-transform duration-200 group-hover:scale-105">
            <svg width="20" height="20" viewBox="0 0 20 20" fill="none">
              <path d="M4 3h9l4 4v11a1 1 0 01-1 1H4a1 1 0 01-1-1V4a1 1 0 011-1z" stroke="#22d3ee" strokeWidth="1.5" strokeLinejoin="round" />
              <path d="M13 3v5h4" stroke="#22d3ee" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
              <path d="M7 10h6M7 13h4" stroke="#22d3ee" strokeWidth="1.5" strokeLinecap="round" />
            </svg>
          </div>
          <div className="hidden sm:block">
            <div className="flex items-center gap-2">
              <h1 className="text-sm font-bold text-white leading-none tracking-tight">ProtocolAI</h1>
              <span className="text-[10px] font-semibold text-cyan-400/70 bg-cyan-400/10 border border-cyan-400/20 px-1.5 py-0.5 rounded-md leading-none">BETA</span>
            </div>
            <p className="text-[11px] text-slate-400 leading-tight mt-0.5">AI-assisted FDA-compliant protocol generation</p>
          </div>
        </Link>

        {/* Primary nav */}
        <nav className="flex items-center gap-1 flex-1 justify-center sm:justify-start sm:ml-4">
          <button onClick={onNavReports} className={navBtn(view === 'reports')}>My Reports</button>
          <button onClick={onNavNew} className={navBtn(view === 'wizard')}>New Protocol</button>
          <button onClick={onNavTeam} className={navBtn(view === 'team')}>Team</button>
        </nav>

        <div className="flex items-center gap-3 flex-shrink-0">
          <ActingUserPicker />
          <AccountButton />
        </div>
      </div>
    </header>
  )
}

export default function ProtocolApp() {
  const [view, setView] = useState<AppView>('reports')
  const [step, setStep] = useState(0)
  const [drug, setDrug] = useState<Drug | null>(null)
  const [studyType, setStudyType] = useState<StudyType | null>(null)
  const [populationConfig, setPopulationConfig] = useState<PopulationConfig>(defaultPopulationConfig)
  const [designDecisions, setDesignDecisions] = useState<DesignDecision[] | null>(null)
  const [protocol, setProtocol] = useState<GeneratedProtocol | null>(null)
  const [openingId, setOpeningId] = useState<string | null>(null)
  const [openError, setOpenError] = useState<string | null>(null)
  // When set, the Clinical Study Report (CSR) flow is shown for this protocol.
  const [csrProtocol, setCsrProtocol] = useState<ProtocolSummary | null>(null)

  const canContinue = ([!!drug, !!studyType, true, !!designDecisions] as const)[step] ?? false
  const meta = STEP_META[step]

  // Changing drug can invalidate a previously chosen study type (e.g. going
  // back from a biosimilar and picking a small-molecule drug) — clear it so
  // the user can't continue with a combination the server would reject.
  const handleDrugChange = (next: Drug | null) => {
    setDrug(next)
    const allowed = allowedStudyTypes(next?.modality)
    if (allowed && studyType && !allowed.includes(studyType)) {
      setStudyType(null)
    }
  }

  const goToReports = () => { setProtocol(null); setCsrProtocol(null); setView('reports') }
  const goToNew = () => {
    setProtocol(null); setCsrProtocol(null)
    setStep(0); setDrug(null); setStudyType(null); setPopulationConfig(defaultPopulationConfig)
    setDesignDecisions(null)
    setView('wizard')
  }
  const goToTeam = () => { setProtocol(null); setCsrProtocol(null); setView('team') }
  // Open the Clinical Study Report flow for a past protocol (post-study results).
  const openCsr = (summary: ProtocolSummary) => { setProtocol(null); setCsrProtocol(summary) }

  // Re-open a past protocol: fetch its full sections, reconstruct the editor's
  // inputs from the summary row (the editor only needs the drug's INN/brands),
  // and drop straight into the editor.
  const openReport = async (summary: ProtocolSummary) => {
    setOpeningId(summary.id)
    setOpenError(null)
    try {
      const full = await api.getProtocol(summary.id)
      setDrug({
        id: summary.drug_id,
        inn: summary.drug_inn,
        brand_names: summary.drug_brand_names,
        rxnorm_id: null,
        updated_at: full.created_at,
      })
      setStudyType(full.study_type)
      setProtocol(full)
    } catch (err) {
      console.error('Failed to open report:', err)
      setOpenError(err instanceof Error ? err.message : 'Failed to open that report. Please try again.')
    } finally {
      setOpeningId(null)
    }
  }

  if (protocol && drug && studyType) {
    return (
      <ProtocolEditor
        protocol={protocol}
        drug={drug}
        onBack={() => setProtocol(null)}
      />
    )
  }

  if (csrProtocol) {
    return (
      <div className="app-canvas min-h-screen">
        <Header view="reports" onNavReports={goToReports} onNavNew={goToNew} onNavTeam={goToTeam} />
        <CSRView protocol={csrProtocol} onBack={goToReports} />
      </div>
    )
  }

  return (
    <div className="app-canvas min-h-screen">
      <Header view={view} onNavReports={goToReports} onNavNew={goToNew} onNavTeam={goToTeam} />

      {view === 'reports' ? (
        <ReportsView onOpen={openReport} onNew={goToNew} onOpenCsr={openCsr} openingId={openingId} openError={openError} onDismissOpenError={() => setOpenError(null)} />
      ) : view === 'team' ? (
        <TeamView />
      ) : (
      <>
      {/* Subtle top-bar progress line */}
      <div className="h-0.5 bg-slate-200">
        <div
          className="h-full bg-gradient-to-r from-cyan-500 to-cyan-400 transition-all duration-500 ease-out"
          style={{ width: `${((step) / (STEPS.length - 1)) * 100}%` }}
        />
      </div>

      <main className="max-w-2xl mx-auto px-4 py-10 space-y-8">
        {/* Stepper */}
        <Stepper steps={STEPS} currentStep={step} />

        {/* Content card */}
        <div className="bg-white rounded-2xl border border-slate-200/80 shadow-[0_6px_28px_-8px_rgba(15,23,42,0.14),0_2px_6px_-2px_rgba(15,23,42,0.06)] fade-up" key={step}>
          {/* Card header */}
          <div className="px-8 pt-7 pb-5 border-b border-slate-100">
            <div className="flex items-center gap-3 mb-3">
              <div className="w-9 h-9 rounded-xl bg-cyan-50 border border-cyan-100 flex items-center justify-center text-cyan-600 flex-shrink-0">
                {meta.icon}
              </div>
              <div>
                <p className="text-[11px] font-semibold text-cyan-600 uppercase tracking-widest leading-none mb-1">
                  Step {step + 1} of {STEPS.length}
                </p>
                <h2 className="text-lg font-bold text-slate-900 leading-tight">{meta.title}</h2>
              </div>
            </div>
            <p className="text-sm text-slate-500 leading-relaxed">{meta.subtitle}</p>
          </div>

          {/* Card body */}
          <div className="px-8 py-7">
            {step === 0 && <DrugSelector value={drug} onChange={handleDrugChange} />}
            {step === 1 && <StudyTypeSelector value={studyType} onChange={setStudyType} drug={drug} />}
            {step === 2 && (
              <PopulationBuilder
                value={populationConfig}
                onChange={setPopulationConfig}
                drug={drug}
                studyType={studyType}
              />
            )}
            {step === 3 && drug && studyType && (
              <ReviewDesign
                drug={drug}
                studyType={studyType}
                populationConfig={populationConfig}
                decisions={designDecisions}
                onChange={setDesignDecisions}
              />
            )}
            {step === 4 && drug && studyType && (
              <GenerateButton
                drug={drug}
                studyType={studyType}
                populationConfig={populationConfig}
                designDecisions={designDecisions ?? []}
                onSuccess={result => setProtocol(result)}
              />
            )}
          </div>
        </div>

        {/* Navigation */}
        {step < 4 && (
          <div className="flex items-center justify-between">
            <button
              onClick={() => setStep(s => Math.max(0, s - 1))}
              disabled={step === 0}
              className="btn-ghost"
            >
              <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
                <path d="M9 3L4 7l5 4" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
              Back
            </button>

            <button
              onClick={() => setStep(s => Math.min(4, s + 1))}
              disabled={!canContinue}
              className="btn-primary"
            >
              {step === 3 ? 'Review & Generate' : 'Continue'}
              <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
                <path d="M5 3l5 4-5 4" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            </button>
          </div>
        )}

        {step === 4 && !protocol && (
          <button onClick={() => setStep(3)} className="btn-ghost">
            <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
              <path d="M9 3L4 7l5 4" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
            Back to Review Design
          </button>
        )}

        {/* Compliance footer */}
        <p className="text-center text-[11px] text-slate-400 pb-4 leading-relaxed">
          Protocols are for investigational use only and require review by a qualified clinical pharmacologist.
          <br />
          FDA BE 2014 · ICH E6(R2) GCP · 21 CFR Part 11
        </p>
      </main>
      </>
      )}
    </div>
  )
}
