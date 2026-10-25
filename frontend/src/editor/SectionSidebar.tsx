import { SECTION_DISPLAY_NAMES } from '../types'
import type { SectionName } from '../types'
import { Check, cx } from '../ui'

interface Props {
  order: readonly SectionName[]
  sections: Record<string, string>
  activeSection: string
  onScrollTo: (sectionName: string) => void
}

/** Section list, styled like the app sidebar (active item: bg-ink-100, rounded-xl). */
export default function SectionSidebar({ order, sections, activeSection, onScrollTo }: Props) {
  const completedCount = order.filter(n => sections[n]?.trim()).length
  const pct = order.length === 0 ? 0 : Math.round((completedCount / order.length) * 100)

  return (
    <aside
      aria-label="Protocol sections"
      className="hidden w-60 flex-none flex-col overflow-hidden border-r border-line bg-surface md:flex"
    >
      <div className="flex-none px-4 pb-3 pt-4">
        <h2 className="eyebrow">Sections</h2>
        <p className="mt-1 text-small text-fg-muted tabular-nums">
          <span className="font-semibold text-fg">{completedCount}</span>/{order.length} drafted
        </p>
        <div
          role="progressbar"
          aria-label="Sections drafted"
          aria-valuemin={0}
          aria-valuemax={order.length}
          aria-valuenow={completedCount}
          className="mt-2 h-1 overflow-hidden rounded-full bg-ink-100"
        >
          <div className="h-full rounded-full bg-accent-600 transition-all duration-panel" style={{ width: `${pct}%` }} />
        </div>
      </div>

      <nav aria-label="Jump to section" className="min-h-0 flex-1 overflow-y-auto px-3 pb-4">
        <ul className="flex flex-col gap-0.5">
          {order.map((name, idx) => {
            const isActive = activeSection === name
            const hasContent = !!sections[name]?.trim()
            return (
              <li key={name}>
                <button
                  type="button"
                  onClick={() => onScrollTo(name)}
                  aria-current={isActive ? 'location' : undefined}
                  className={cx(
                    'flex h-9 w-full items-center gap-2.5 rounded-xl px-3 text-left text-body transition-colors duration-fast',
                    isActive ? 'bg-ink-100 font-medium text-fg' : 'text-fg-muted hover:bg-surface-hover hover:text-fg',
                  )}
                >
                  <span
                    aria-hidden
                    className={cx(
                      'h-1.5 w-1.5 flex-none rounded-full',
                      hasContent ? 'bg-accent-600' : 'bg-ink-300',
                    )}
                  />
                  <span className="min-w-0 flex-1 truncate">
                    <span className="text-small tabular-nums text-fg-subtle">{idx + 1}.</span>{' '}
                    {SECTION_DISPLAY_NAMES[name as SectionName]}
                  </span>
                  {hasContent ? (
                    <Check aria-hidden className="h-3.5 w-3.5 flex-none text-fg-subtle" />
                  ) : null}
                  <span className="sr-only">{hasContent ? ' (drafted)' : ' (empty)'}</span>
                </button>
              </li>
            )
          })}
        </ul>
      </nav>
    </aside>
  )
}
