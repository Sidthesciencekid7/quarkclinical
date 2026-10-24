import {
  Children,
  cloneElement,
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type KeyboardEvent,
  type MouseEvent,
  type ReactElement,
  type ReactNode,
  type Ref,
} from 'react'
import { createPortal } from 'react-dom'
import { cx, mergeRefs } from './cx'

export interface MenuItemDef {
  type?: 'item'
  id: string
  label: ReactNode
  /** Leading 16px icon. */
  icon?: ReactNode
  /** Right-aligned hint (e.g. a shortcut or a badge). */
  trailing?: ReactNode
  /** Called when chosen (click, Enter, Space). */
  onSelect?: () => void
  /** Renders an anchor instead of a button. */
  href?: string
  target?: string
  danger?: boolean
  disabled?: boolean
}
export interface MenuSeparatorDef {
  type: 'separator'
  id: string
}
export interface MenuLabelDef {
  type: 'label'
  id: string
  label: ReactNode
}
export type MenuEntry = MenuItemDef | MenuSeparatorDef | MenuLabelDef

export type MenuPlacement = 'bottom-start' | 'bottom-end' | 'top-start' | 'top-end'

export interface MenuProps {
  /**
   * The element that opens the menu. It must forward `ref` and spread
   * `onClick` / `onKeyDown` / aria props (Button, IconButton and Chip do).
   */
  trigger: ReactElement
  items: MenuEntry[]
  /** Accessible name of the menu. */
  label: string
  /** Default `bottom-start`. Flips vertically when there is no room. */
  placement?: MenuPlacement
  /** Minimum panel width in px (default 224). */
  minWidth?: number
  /** Non-interactive content above the items (e.g. org name and role). */
  header?: ReactNode
  onOpenChange?: (open: boolean) => void
  className?: string
}

const itemClass =
  'flex w-full items-center gap-2.5 rounded-xl px-2.5 py-2 text-left text-body no-underline outline-none transition-colors duration-fast hover:bg-surface-hover focus:bg-surface-hover aria-disabled:cursor-not-allowed aria-disabled:opacity-40 [&>svg]:h-4 [&>svg]:w-4 [&>svg]:flex-none'

/**
 * Popover menu. Opens on click or ArrowDown/ArrowUp; arrow keys, Home/End and
 * type-ahead move; Esc closes and returns focus to the trigger. Rendered in a
 * portal so it is never clipped by an `overflow-hidden` parent.
 */
export function Menu({
  trigger,
  items,
  label,
  placement = 'bottom-start',
  minWidth = 224,
  header,
  onOpenChange,
  className,
}: MenuProps) {
  const [open, setOpenState] = useState(false)
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null)
  const [surface, setSurface] = useState<'light' | 'dark'>('light')
  const triggerRef = useRef<HTMLElement | null>(null)
  const panelRef = useRef<HTMLDivElement | null>(null)
  const initialFocus = useRef<'first' | 'last'>('first')
  const typeahead = useRef({ text: '', timer: 0 as unknown as ReturnType<typeof setTimeout> })
  const menuId = useId()

  const openRef = useRef(false)
  openRef.current = open

  const setOpen = useCallback(
    (next: boolean) => {
      if (openRef.current !== next) onOpenChange?.(next)
      setOpenState(next)
      if (!next) setPos(null)
    },
    [onOpenChange],
  )

  const close = useCallback(
    (returnFocus: boolean) => {
      setOpen(false)
      if (returnFocus) triggerRef.current?.focus()
    },
    [setOpen],
  )

  const openMenu = (focus: 'first' | 'last') => {
    initialFocus.current = focus
    const surf = triggerRef.current?.closest('[data-surface]')?.getAttribute('data-surface')
    setSurface(surf === 'dark' ? 'dark' : 'light')
    setOpen(true)
  }

  const enabledItems = () =>
    Array.from(
      panelRef.current?.querySelectorAll<HTMLElement>('[role="menuitem"]:not([aria-disabled="true"])') ?? [],
    )

  // Position next to the trigger; flip if it would overflow the viewport.
  const place = useCallback(() => {
    const t = triggerRef.current
    const p = panelRef.current
    if (!t || !p) return
    const r = t.getBoundingClientRect()
    const pw = p.offsetWidth
    const ph = p.offsetHeight
    const vw = window.innerWidth
    const vh = window.innerHeight
    const gap = 6
    const margin = 8
    let vertical: 'top' | 'bottom' = placement.startsWith('top') ? 'top' : 'bottom'
    if (vertical === 'bottom' && r.bottom + gap + ph > vh - margin && r.top - gap - ph >= margin) vertical = 'top'
    else if (vertical === 'top' && r.top - gap - ph < margin && r.bottom + gap + ph <= vh - margin) vertical = 'bottom'
    const top = vertical === 'bottom' ? r.bottom + gap : r.top - gap - ph
    let left = placement.endsWith('end') ? r.right - pw : r.left
    left = Math.max(margin, Math.min(left, vw - pw - margin))
    setPos(prev => (prev && prev.top === top && prev.left === left ? prev : { top, left }))
  }, [placement])

  useLayoutEffect(() => {
    if (open) place()
  }, [open, place, items])

  // Focus the first/last item once positioned.
  const positioned = pos !== null
  useLayoutEffect(() => {
    if (!open || !positioned) return
    const list = enabledItems()
    ;(initialFocus.current === 'last' ? list[list.length - 1] : list[0])?.focus()
    // Only on the transition to "positioned".
  }, [open, positioned])

  useEffect(() => {
    if (!open) return
    const reposition = () => place()
    const onDown = (e: globalThis.MouseEvent) => {
      const node = e.target as Node
      if (panelRef.current?.contains(node) || triggerRef.current?.contains(node)) return
      close(false)
    }
    window.addEventListener('resize', reposition)
    window.addEventListener('scroll', reposition, true)
    document.addEventListener('mousedown', onDown)
    return () => {
      window.removeEventListener('resize', reposition)
      window.removeEventListener('scroll', reposition, true)
      document.removeEventListener('mousedown', onDown)
    }
  }, [open, place, close])

  const onPanelKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    const list = enabledItems()
    const idx = list.indexOf(document.activeElement as HTMLElement)
    const focusAt = (i: number) => list[(i + list.length) % list.length]?.focus()
    switch (e.key) {
      case 'ArrowDown':
        e.preventDefault()
        focusAt(idx + 1)
        return
      case 'ArrowUp':
        e.preventDefault()
        focusAt(idx < 0 ? -1 : idx - 1)
        return
      case 'Home':
        e.preventDefault()
        focusAt(0)
        return
      case 'End':
        e.preventDefault()
        focusAt(-1)
        return
      case 'Escape':
        e.preventDefault()
        e.stopPropagation() // do not also close a Modal underneath
        close(true)
        return
      case 'Tab':
        e.preventDefault()
        close(true)
        return
      case ' ':
        // Anchors do not activate on Space natively.
        if ((document.activeElement as HTMLElement | null)?.tagName === 'A') {
          e.preventDefault()
          ;(document.activeElement as HTMLElement).click()
        }
        return
      default:
        break
    }
    // Type-ahead: jump to the next item whose label starts with the typed text.
    if (e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) {
      const ta = typeahead.current
      clearTimeout(ta.timer)
      ta.text += e.key.toLowerCase()
      ta.timer = setTimeout(() => {
        ta.text = ''
      }, 500)
      const ordered = [...list.slice(idx + 1), ...list.slice(0, idx + 1)]
      ordered.find(el => (el.textContent ?? '').trim().toLowerCase().startsWith(ta.text))?.focus()
    }
  }

  const select = (item: MenuItemDef) => (e: MouseEvent<HTMLElement>) => {
    if (item.disabled) {
      e.preventDefault()
      return
    }
    item.onSelect?.()
    close(true)
  }

  const child = Children.only(trigger) as ReactElement<Record<string, unknown>> & { ref?: Ref<HTMLElement> }
  const childProps = child.props as {
    onClick?: (e: MouseEvent<HTMLElement>) => void
    onKeyDown?: (e: KeyboardEvent<HTMLElement>) => void
  }
  const cloned = cloneElement(child, {
    ref: mergeRefs(child.ref, triggerRef),
    'aria-haspopup': 'menu',
    'aria-expanded': open,
    'aria-controls': open ? menuId : undefined,
    onClick: (e: MouseEvent<HTMLElement>) => {
      childProps.onClick?.(e)
      if (e.defaultPrevented) return
      if (open) close(false)
      else openMenu('first')
    },
    onKeyDown: (e: KeyboardEvent<HTMLElement>) => {
      childProps.onKeyDown?.(e)
      if (e.defaultPrevented) return
      if (e.key === 'ArrowDown') {
        e.preventDefault()
        openMenu('first')
      } else if (e.key === 'ArrowUp') {
        e.preventDefault()
        openMenu('last')
      }
    },
  })

  return (
    <>
      {cloned}
      {open
        ? createPortal(
            <div
              ref={panelRef}
              id={menuId}
              role="menu"
              aria-label={label}
              aria-orientation="vertical"
              data-surface={surface}
              onKeyDown={onPanelKeyDown}
              style={{
                position: 'fixed',
                top: pos?.top ?? 0,
                left: pos?.left ?? 0,
                minWidth,
                visibility: pos ? 'visible' : 'hidden',
              }}
              className={cx(
                'z-[80] max-h-[min(70vh,28rem)] overflow-y-auto rounded-2xl border border-line bg-surface-pop p-1.5 text-fg shadow-pop animate-ui-pop-in',
                className,
              )}
            >
              {header ? <div className="px-2.5 py-2">{header}</div> : null}
              {items.map(entry => {
                if (entry.type === 'separator') {
                  return <div key={entry.id} role="separator" className="-mx-1.5 my-1.5 h-px bg-line" />
                }
                if (entry.type === 'label') {
                  return (
                    <div key={entry.id} role="presentation" className="eyebrow px-2.5 pb-1 pt-2">
                      {entry.label}
                    </div>
                  )
                }
                const common = {
                  role: 'menuitem' as const,
                  tabIndex: -1,
                  'aria-disabled': entry.disabled ? (true as const) : undefined,
                  className: cx(
                    itemClass,
                    entry.danger ? 'text-[color:var(--btn-danger-fg)]' : 'text-fg [&>svg]:text-fg-muted',
                  ),
                  onClick: select(entry),
                  onMouseMove: (e: MouseEvent<HTMLElement>) => {
                    if (!entry.disabled && document.activeElement !== e.currentTarget) e.currentTarget.focus()
                  },
                }
                const inner = (
                  <>
                    {entry.icon}
                    <span className="min-w-0 flex-1 truncate">{entry.label}</span>
                    {entry.trailing}
                  </>
                )
                return entry.href && !entry.disabled ? (
                  <a key={entry.id} href={entry.href} target={entry.target} rel={entry.target === '_blank' ? 'noopener noreferrer' : undefined} {...common}>
                    {inner}
                  </a>
                ) : (
                  <button key={entry.id} type="button" {...common}>
                    {inner}
                  </button>
                )
              })}
            </div>,
            document.body,
          )
        : null}
    </>
  )
}
