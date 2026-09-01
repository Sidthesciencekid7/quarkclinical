import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'
import QuarkLogo from '../components/QuarkLogo'
import { AccountButton } from '../lib/auth'
import { ActingUserPicker } from '../lib/actingUser'

type ToolStatus = 'available' | 'coming_soon'

interface Tool {
  id: string
  name: string
  description: string
  icon: ReactNode
  /** Present only for available tools — coming-soon tiles have nowhere to go yet. */
  href?: string
  status: ToolStatus
}

const ProtocolIcon = (
  <svg width="26" height="26" viewBox="0 0 20 20" fill="none">
    <path d="M4 3h9l4 4v11a1 1 0 01-1 1H4a1 1 0 01-1-1V4a1 1 0 011-1z" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
    <path d="M13 3v5h4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
    <path d="M7 10h6M7 13h4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
  </svg>
)

const BudgetIcon = (
  <svg width="26" height="26" viewBox="0 0 20 20" fill="none">
    <rect x="2" y="5" width="16" height="11" rx="2" stroke="currentColor" strokeWidth="1.5" />
    <path d="M2 8.5h16" stroke="currentColor" strokeWidth="1.5" />
    <circle cx="14.5" cy="12" r="1.25" fill="currentColor" />
  </svg>
)

const LockIcon = (
  <svg width="10" height="10" viewBox="0 0 20 20" fill="none" className="flex-shrink-0">
    <rect x="5" y="9" width="10" height="7" rx="1.5" stroke="currentColor" strokeWidth="1.6" />
    <path d="M7 9V6a3 3 0 016 0v3" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
  </svg>
)

const TOOLS: Tool[] = [
  {
    id: 'protocol',
    name: 'Protocol Generator',
    description: 'AI-assisted, statistically-rigorous FDA-compliant study protocols.',
    icon: ProtocolIcon,
    href: '/app/protocol',
    status: 'available',
  },
  {
    id: 'budgeting',
    name: 'Budgeting Tool',
    description: 'Plan and track study budgets across sites, vendors, and CROs.',
    icon: BudgetIcon,
    status: 'coming_soon',
  },
]

function ToolCard({ tool }: { tool: Tool }) {
  const iconWrap = tool.status === 'available'
    ? 'bg-cyan-50 border-cyan-100 text-cyan-600 group-hover:scale-105'
    : 'bg-slate-100 border-slate-200 text-slate-400'

  const inner = (
    <>
      {tool.status === 'coming_soon' && (
        <span className="absolute top-3.5 right-3.5 inline-flex items-center gap-1 text-[10px] font-semibold text-amber-700 bg-amber-100 border border-amber-200 px-1.5 py-0.5 rounded-md leading-none">
          {LockIcon}
          Coming soon
        </span>
      )}
      <div className={`w-14 h-14 rounded-2xl border flex items-center justify-center flex-shrink-0 transition-transform duration-300 ${iconWrap}`}>
        {tool.icon}
      </div>
      <h2 className={`text-base font-bold text-center leading-tight ${tool.status === 'available' ? 'text-slate-900' : 'text-slate-500'}`}>
        {tool.name}
      </h2>
      <p className={`text-xs text-center leading-relaxed max-w-[190px] ${tool.status === 'available' ? 'text-slate-500' : 'text-slate-400'}`}>
        {tool.description}
      </p>
    </>
  )

  if (tool.status === 'available' && tool.href) {
    return (
      <Link
        to={tool.href}
        className="group relative aspect-square rounded-2xl border border-slate-200/80 bg-white shadow-[0_6px_28px_-8px_rgba(15,23,42,0.10),0_2px_6px_-2px_rgba(15,23,42,0.05)] card-lift hover:shadow-[0_14px_36px_-10px_rgba(15,23,42,0.18)] hover:border-cyan-300/60 transition-all duration-300 flex flex-col items-center justify-center gap-4 p-8 cursor-pointer focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-400/60 focus-visible:ring-offset-2"
      >
        {inner}
      </Link>
    )
  }

  return (
    <button
      type="button"
      disabled
      aria-label={`${tool.name} — coming soon, not yet available`}
      className="relative aspect-square rounded-2xl border border-slate-200/80 bg-slate-50/60 flex flex-col items-center justify-center gap-4 p-8 opacity-80 cursor-not-allowed"
    >
      {inner}
    </button>
  )
}

export default function SuiteHome() {
  return (
    <div className="app-canvas min-h-screen">
      <header style={{ background: 'var(--navy)' }} className="border-b border-white/5 sticky top-0 z-30">
        <div className="max-w-5xl mx-auto px-6 py-3 flex items-center justify-between gap-4">
          <div className="flex items-center gap-3 flex-shrink-0">
            <div className="w-9 h-9 rounded-xl bg-cyan-500/15 border border-cyan-400/25 flex items-center justify-center flex-shrink-0">
              <QuarkLogo size={20} stroke="#22d3ee" />
            </div>
            <h1 className="text-sm font-bold text-white leading-none tracking-tight">Quark AI</h1>
          </div>

          <div className="flex items-center gap-3 flex-shrink-0">
            <ActingUserPicker />
            <AccountButton />
          </div>
        </div>
      </header>

      <main className="max-w-4xl mx-auto px-6 py-16">
        <div className="text-center mb-12 fade-up">
          <h2 className="text-2xl sm:text-3xl font-extrabold text-slate-900 tracking-tight mb-2">Choose a tool</h2>
          <p className="text-sm text-slate-500">Everything in your Quark AI workspace, in one place.</p>
        </div>

        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-6 max-w-3xl mx-auto fade-up">
          {TOOLS.map(tool => <ToolCard key={tool.id} tool={tool} />)}
        </div>

        <p className="text-center text-xs text-slate-400 mt-14">More tools are on the way.</p>
      </main>
    </div>
  )
}
