import { SECTION_DISPLAY_NAMES } from '../types'
import type { SectionName } from '../types'

interface Props {
  order: readonly SectionName[]
  sections: Record<string, string>
  activeSection: string
  onScrollTo: (sectionName: string) => void
}

export default function SectionSidebar({ order, sections, activeSection, onScrollTo }: Props) {
  const completedCount = order.filter(n => sections[n]?.trim()).length

  return (
    <aside
      className="w-56 flex-shrink-0 overflow-y-auto border-r border-white/8 flex flex-col"
      style={{ background: 'var(--navy)' }}
    >
      {/* Header */}
      <div className="px-4 pt-5 pb-3 border-b border-white/8 flex-shrink-0">
        <p className="text-[10px] font-bold text-slate-500 uppercase tracking-widest mb-0.5">Sections</p>
        <p className="text-xs text-slate-400 font-medium">
          <span className="text-emerald-400 font-bold">{completedCount}</span>
          <span className="text-slate-500">/{order.length} complete</span>
        </p>
      </div>

      {/* Progress bar */}
      <div className="px-4 py-2 flex-shrink-0">
        <div className="h-1 bg-white/8 rounded-full overflow-hidden">
          <div
            className="h-full bg-gradient-to-r from-cyan-500 to-emerald-400 rounded-full transition-all duration-500"
            style={{ width: `${(completedCount / order.length) * 100}%` }}
          />
        </div>
      </div>

      {/* Nav items */}
      <nav className="flex-1 px-2 pb-4 space-y-0.5 overflow-y-auto">
        {order.map((name, idx) => {
          const isActive = activeSection === name
          const hasContent = !!(sections[name]?.trim())

          return (
            <button
              key={name}
              onClick={() => onScrollTo(name)}
              className={[
                'w-full text-left px-3 py-2.5 rounded-xl text-xs flex items-center gap-2.5 transition-all duration-150 cursor-pointer group',
                isActive
                  ? 'nav-item-active bg-cyan-500/15 text-cyan-200 ring-1 ring-cyan-400/20'
                  : 'text-slate-400 hover:bg-white/5 hover:text-slate-200',
              ].join(' ')}
            >
              {/* Status dot */}
              <span className={[
                'flex-shrink-0 w-1.5 h-1.5 rounded-full transition-colors',
                hasContent
                  ? isActive ? 'bg-cyan-400' : 'bg-emerald-400/70'
                  : isActive ? 'bg-cyan-600' : 'bg-white/15',
              ].join(' ')} />

              <span className="flex-1 min-w-0 truncate leading-snug">
                <span className={[
                  'font-mono mr-1 text-[10px]',
                  isActive ? 'text-cyan-500' : 'text-slate-600',
                ].join(' ')}>
                  {idx + 1}.
                </span>
                <span className="font-medium">
                  {SECTION_DISPLAY_NAMES[name as SectionName]}
                </span>
              </span>

              {hasContent && !isActive && (
                <svg width="10" height="10" viewBox="0 0 10 10" fill="none" className="flex-shrink-0 opacity-50">
                  <path d="M1.5 5l2.5 2.5 4.5-5" stroke="#34d399" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
                </svg>
              )}
            </button>
          )
        })}
      </nav>
    </aside>
  )
}
