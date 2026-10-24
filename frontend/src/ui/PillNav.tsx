import type { ReactElement, ReactNode } from 'react'
import { cx } from './cx'

export interface PillNavItem {
  id: string
  label: ReactNode
  /** Leading 16px icon, e.g. `<Search />`. */
  icon?: ReactNode
  /** `nav` mode: renders an anchor to this URL (unless `renderItem` is used). */
  href?: string
  disabled?: boolean
  /** Use when `label` is not plain text. */
  ariaLabel?: string
}

/** What `renderItem` receives, so a router `<Link>` can take over rendering. */
export interface PillNavItemRenderProps {
  className: string
  children: ReactNode
  'aria-current'?: 'page' | 'true' | 'location'
  'aria-pressed'?: boolean
  'aria-label'?: string
  'aria-disabled'?: true
  href?: string
  onClick: () => void
}

export interface PillNavProps {
  items: PillNavItem[]
  /** Id of the active item. */
  value: string
  /** Called with the item id when an item is activated. */
  onChange?: (id: string) => void
  /**
   * `toggle` (default): a toggle-button group, active item has `aria-pressed`.
   * `nav`: navigation, active item has `aria-current`.
   */
  mode?: 'toggle' | 'nav'
  /** aria-current token used in `nav` mode. Default `page`. */
  current?: 'page' | 'true' | 'location'
  /** Accessible name for the group / navigation landmark. */
  ariaLabel: string
  size?: 'sm' | 'md'
  className?: string
  /** Custom item renderer (e.g. react-router `Link`). Must apply `className` and `children`. */
  renderItem?: (item: PillNavItem, props: PillNavItemRenderProps) => ReactElement
}

/**
 * Segmented pill navigation (the NORMA header control). Works on light and dark
 * surfaces; on dark it is translucent glass.
 */
export function PillNav({
  items,
  value,
  onChange,
  mode = 'toggle',
  current = 'page',
  ariaLabel,
  size = 'md',
  className,
  renderItem,
}: PillNavProps) {
  const containerClass = cx('pillnav', size === 'sm' && 'pillnav-sm', className)

  const content = items.map(item => {
    const active = item.id === value
    const children = (
      <>
        {item.icon}
        {item.label}
      </>
    )
    const props: PillNavItemRenderProps = {
      className: 'pillnav-item',
      children,
      'aria-label': item.ariaLabel,
      'aria-disabled': item.disabled ? true : undefined,
      onClick: () => {
        if (!item.disabled) onChange?.(item.id)
      },
      ...(mode === 'toggle'
        ? { 'aria-pressed': active }
        : { 'aria-current': active ? current : undefined, href: item.href }),
    }

    if (renderItem) return <Fragmented key={item.id}>{renderItem(item, props)}</Fragmented>

    if (mode === 'nav' && item.href && !item.disabled) {
      return (
        <a
          key={item.id}
          href={item.href}
          className={props.className}
          aria-current={props['aria-current']}
          aria-label={props['aria-label']}
          onClick={() => onChange?.(item.id)}
        >
          {children}
        </a>
      )
    }
    return (
      <button
        key={item.id}
        type="button"
        className={props.className}
        aria-pressed={props['aria-pressed']}
        aria-current={props['aria-current']}
        aria-label={props['aria-label']}
        aria-disabled={props['aria-disabled']}
        disabled={item.disabled}
        onClick={props.onClick}
      >
        {children}
      </button>
    )
  })

  if (mode === 'nav') {
    return (
      <nav aria-label={ariaLabel} className={containerClass}>
        {content}
      </nav>
    )
  }
  return (
    <div role="group" aria-label={ariaLabel} className={containerClass}>
      {content}
    </div>
  )
}

// A keyed passthrough so `renderItem` results do not need their own `key`.
function Fragmented({ children }: { children: ReactNode }) {
  return <>{children}</>
}
