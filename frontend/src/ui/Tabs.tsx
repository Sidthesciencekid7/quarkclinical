import { useId, useRef, type KeyboardEvent, type ReactNode } from 'react'
import { cx } from './cx'

export interface TabItem {
  id: string
  label: ReactNode
  icon?: ReactNode
  /** Trailing count or badge. */
  badge?: ReactNode
  disabled?: boolean
}

export interface TabsProps {
  items: TabItem[]
  value: string
  onChange: (id: string) => void
  /** `underline` (Explore / History style) or `pill` (segmented). Default `underline`. */
  variant?: 'underline' | 'pill'
  ariaLabel: string
  /**
   * Base id used to link tabs to their `TabPanel`s. Pass the same string to
   * `<TabPanel tabsId=...>`. Defaults to a generated id.
   */
  id?: string
  className?: string
}

export const tabDomId = (tabsId: string, itemId: string) => `${tabsId}-tab-${itemId}`
export const panelDomId = (tabsId: string, itemId: string) => `${tabsId}-panel-${itemId}`

/**
 * WAI-ARIA tabs: roving tabindex, Left/Right/Home/End move and activate.
 * Pair with `TabPanel`.
 */
export function Tabs({ items, value, onChange, variant = 'underline', ariaLabel, id, className }: TabsProps) {
  const generated = useId()
  const tabsId = id ?? generated
  const listRef = useRef<HTMLDivElement>(null)

  const enabled = items.filter(i => !i.disabled)

  const move = (e: KeyboardEvent<HTMLDivElement>) => {
    const idx = enabled.findIndex(i => i.id === value)
    let next = -1
    if (e.key === 'ArrowRight') next = (idx + 1) % enabled.length
    else if (e.key === 'ArrowLeft') next = (idx - 1 + enabled.length) % enabled.length
    else if (e.key === 'Home') next = 0
    else if (e.key === 'End') next = enabled.length - 1
    if (next < 0 || enabled.length === 0) return
    e.preventDefault()
    const target = enabled[next]
    onChange(target.id)
    listRef.current?.querySelector<HTMLElement>(`[id="${tabDomId(tabsId, target.id)}"]`)?.focus()
  }

  return (
    <div
      ref={listRef}
      role="tablist"
      aria-label={ariaLabel}
      onKeyDown={move}
      className={cx(variant === 'pill' ? 'pillnav' : 'tabs-underline', className)}
    >
      {items.map(item => {
        const selected = item.id === value
        return (
          <button
            key={item.id}
            id={tabDomId(tabsId, item.id)}
            type="button"
            role="tab"
            aria-selected={selected}
            aria-controls={panelDomId(tabsId, item.id)}
            aria-disabled={item.disabled || undefined}
            disabled={item.disabled}
            tabIndex={selected ? 0 : -1}
            className={variant === 'pill' ? 'pillnav-item' : 'tab'}
            onClick={() => onChange(item.id)}
          >
            {item.icon}
            {item.label}
            {item.badge}
          </button>
        )
      })}
    </div>
  )
}

export interface TabPanelProps {
  /** Same string given to `<Tabs id>`. */
  tabsId: string
  /** The tab id this panel belongs to. */
  id: string
  /** Current `Tabs` value; the panel shows only when it equals `id`. */
  value: string
  /** Keep the panel mounted (hidden) while inactive. Default false. */
  keepMounted?: boolean
  className?: string
  children: ReactNode
}

export function TabPanel({ tabsId, id, value, keepMounted = false, className, children }: TabPanelProps) {
  const active = id === value
  if (!active && !keepMounted) return null
  return (
    <div
      role="tabpanel"
      id={panelDomId(tabsId, id)}
      aria-labelledby={tabDomId(tabsId, id)}
      hidden={!active}
      tabIndex={0}
      className={cx('focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[color:var(--ring)] focus-visible:ring-offset-2 rounded-input', className)}
    >
      {children}
    </div>
  )
}
