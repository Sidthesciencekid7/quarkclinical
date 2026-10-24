import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import QuarkLogo from './components/QuarkLogo'
import { AccountButton } from './lib/auth'
import { ActingUserPicker } from './lib/actingUser'
import { api } from './api/client'
import BudgetList from './components/BudgetList'
import BudgetCreateView from './components/BudgetCreateView'
import BudgetEditor from './components/BudgetEditor'
import type { Budget, BudgetSummary } from './types'

type BudgetView = 'list' | 'new'

function Header({ view, onNavList, onNavNew }: {
  view: BudgetView | 'editor'
  onNavList: () => void
  onNavNew: () => void
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
        <Link to="/app" className="flex items-center gap-3 group flex-shrink-0" title="Suite home">
          <div className="w-9 h-9 rounded-xl bg-cyan-500/15 border border-cyan-400/25 flex items-center justify-center flex-shrink-0 transition-transform duration-200 group-hover:scale-105">
            <QuarkLogo size={20} stroke="#22d3ee" />
          </div>
          <div className="hidden sm:block">
            <div className="flex items-center gap-2">
              <h1 className="text-sm font-bold text-white leading-none tracking-tight">QuarkClinical</h1>
              <span className="text-[10px] font-semibold text-cyan-400/70 bg-cyan-400/10 border border-cyan-400/20 px-1.5 py-0.5 rounded-md leading-none">BETA</span>
            </div>
            <p className="text-[11px] text-slate-400 leading-tight mt-0.5">Study budget planning and CRO cost build-up</p>
          </div>
        </Link>

        {/* Primary nav */}
        <nav className="flex items-center gap-1 flex-1 justify-center sm:justify-start sm:ml-4">
          <button onClick={onNavList} className={navBtn(view === 'list')}>My Budgets</button>
          <button onClick={onNavNew} className={navBtn(view === 'new')}>New Budget</button>
        </nav>

        <div className="flex items-center gap-3 flex-shrink-0">
          <ActingUserPicker />
          <AccountButton />
        </div>
      </div>
    </header>
  )
}

/** `embedded`: rendered inside the app shell (src/shell), which supplies the
 *  sidebar, top bar, identity controls and the budget list/editor routes. The
 *  tool then starts on the create view, hides its own header, and hands a new
 *  budget to `/app/budgets/:id` (with `justCreated` router state) instead of
 *  opening the editor itself. Temporary: deleted when chat replaces this. */
export default function BudgetingApp({ embedded = false }: { embedded?: boolean }) {
  const navigate = useNavigate()
  const [view, setView] = useState<BudgetView>(embedded ? 'new' : 'list')
  const [openBudget, setOpenBudget] = useState<Budget | null>(null)
  const [openingId, setOpeningId] = useState<string | null>(null)
  const [openError, setOpenError] = useState<string | null>(null)
  // True only for the budget just created this session — shows the editor's
  // one-time "ready" hero banner. Cleared on any other navigation into/out
  // of the editor so it never reappears for a budget opened from the list.
  const [justCreated, setJustCreated] = useState(false)
  // Bumped whenever the editor is left, so BudgetList remounts and re-fetches
  // — grand totals/counts changed inside the editor otherwise wouldn't show
  // up in the list without a manual refresh.
  const [listVersion, setListVersion] = useState(0)

  async function handleOpen(summary: BudgetSummary) {
    setOpeningId(summary.id)
    setOpenError(null)
    try {
      const full = await api.getBudget(summary.id)
      setOpenBudget(full)
      setJustCreated(false)
    } catch (e) {
      setOpenError(e instanceof Error ? e.message : 'Failed to open budget')
    } finally {
      setOpeningId(null)
    }
  }

  function handleBack() {
    setOpenBudget(null)
    setJustCreated(false)
    setListVersion(v => v + 1)
  }

  const headerView: BudgetView | 'editor' = openBudget ? 'editor' : view

  return (
    <div className={embedded ? 'app-canvas min-h-full' : 'app-canvas min-h-screen'}>
      {!embedded && (
        <Header
          view={headerView}
          onNavList={() => { setOpenBudget(null); setView('list') }}
          onNavNew={() => { setOpenBudget(null); setView('new') }}
        />
      )}

      {openBudget ? (
        <BudgetEditor budget={openBudget} onBack={handleBack} onChanged={setOpenBudget} justCreated={justCreated} />
      ) : view === 'new' ? (
        <BudgetCreateView
          onCreated={budget => {
            if (embedded) {
              navigate(`/app/budgets/${budget.id}`, { state: { justCreated: true } })
              return
            }
            setOpenBudget(budget)
            setJustCreated(true)
          }}
          onCancel={() => (embedded ? navigate('/app/budgets') : setView('list'))}
        />
      ) : (
        <BudgetList
          key={listVersion}
          onOpen={handleOpen}
          onNew={() => setView('new')}
          openingId={openingId}
          openError={openError}
          onDismissOpenError={() => setOpenError(null)}
        />
      )}
    </div>
  )
}
