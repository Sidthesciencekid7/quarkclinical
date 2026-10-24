import { useEffect, useRef } from 'react'
import type { KeyboardEvent, ReactNode } from 'react'
import { getFocusable } from '../ui/cx'

export interface MobileDrawerProps {
  open: boolean
  onClose: () => void
  /** Accessible name of the dialog. */
  label: string
  /** Where focus goes when the drawer closes; defaults to the element focused when it opened. */
  returnFocusTo?: () => HTMLElement | null
  children: ReactNode
}

/**
 * Slide-in navigation drawer for small screens. A modal dialog: focus moves in
 * on open, Tab / Shift+Tab stay inside, Esc or a tap on the dimmed backdrop
 * closes it, body scroll is locked, and focus returns to the opener.
 */
export function MobileDrawer({ open, onClose, label, returnFocusTo, children }: MobileDrawerProps) {
  const panelRef = useRef<HTMLDivElement>(null)
  const returnFocusRef = useRef(returnFocusTo)
  returnFocusRef.current = returnFocusTo

  useEffect(() => {
    if (!open) return
    const opener = returnFocusRef.current?.() ?? (document.activeElement as HTMLElement | null)
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'

    const panel = panelRef.current
    const reduceMotion =
      typeof window.matchMedia === 'function' && window.matchMedia('(prefers-reduced-motion: reduce)').matches
    if (panel && !reduceMotion && typeof panel.animate === 'function') {
      panel.animate([{ transform: 'translateX(-100%)' }, { transform: 'translateX(0)' }], {
        duration: 200,
        easing: 'cubic-bezier(0.16, 1, 0.3, 1)',
      })
    }
    ;(getFocusable(panel)[0] ?? panel)?.focus()

    return () => {
      document.body.style.overflow = previousOverflow
      if (opener && document.contains(opener)) opener.focus()
    }
  }, [open])

  if (!open) return null

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key === 'Escape') {
      e.preventDefault()
      onClose()
      return
    }
    if (e.key !== 'Tab') return
    const panel = panelRef.current
    const focusable = getFocusable(panel)
    if (focusable.length === 0) {
      e.preventDefault()
      panel?.focus()
      return
    }
    const first = focusable[0]
    const last = focusable[focusable.length - 1]
    const active = document.activeElement
    if (e.shiftKey && (active === first || active === panel)) {
      e.preventDefault()
      last.focus()
    } else if (!e.shiftKey && active === last) {
      e.preventDefault()
      first.focus()
    } else if (!panel?.contains(active)) {
      e.preventDefault()
      first.focus()
    }
  }

  return (
    <div className="fixed inset-0 z-[60] flex" data-testid="mobile-drawer">
      <div aria-hidden className="absolute inset-0 bg-overlay animate-ui-fade-in" onMouseDown={onClose} />
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-label={label}
        tabIndex={-1}
        onKeyDown={onKeyDown}
        className="relative flex h-full w-[288px] max-w-[85vw] flex-col border-r border-line bg-surface shadow-pop outline-none"
      >
        {children}
      </div>
    </div>
  )
}
