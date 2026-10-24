import {
  useEffect,
  useId,
  useRef,
  type KeyboardEvent,
  type MouseEvent,
  type ReactNode,
  type RefObject,
} from 'react'
import { createPortal } from 'react-dom'
import { cx, getFocusable } from './cx'
import { IconButton } from './IconButton'
import { X } from './icons'

export interface ModalProps {
  open: boolean
  onClose: () => void
  /** Required: names the dialog (aria-labelledby). */
  title: ReactNode
  description?: ReactNode
  children?: ReactNode
  /** Footer row, right-aligned (pill buttons). */
  footer?: ReactNode
  size?: 'sm' | 'md' | 'lg'
  /** Render the panel on the dark navy surface (landing). Default light. */
  surface?: 'light' | 'dark'
  /** Click on the dimmed backdrop closes (default true). */
  dismissOnBackdrop?: boolean
  /** Hide the corner close button (the dialog still closes on Esc). */
  hideClose?: boolean
  /**
   * Element to focus on open. Default: an element marked `data-autofocus`,
   * else the first focusable in the body, else the first in the dialog.
   */
  initialFocusRef?: RefObject<HTMLElement>
  className?: string
}

const SIZE: Record<NonNullable<ModalProps['size']>, string> = {
  sm: 'max-w-sm',
  md: 'max-w-lg',
  lg: 'max-w-2xl',
}

// Only the top-most modal reacts to Esc.
const openStack: symbol[] = []

/**
 * Accessible dialog: `role="dialog"` + `aria-modal`, focus is trapped inside,
 * Esc closes, focus returns to the element that opened it, body scroll locks.
 */
export function Modal({
  open,
  onClose,
  title,
  description,
  children,
  footer,
  size = 'md',
  surface = 'light',
  dismissOnBackdrop = true,
  hideClose = false,
  initialFocusRef,
  className,
}: ModalProps) {
  const titleId = useId()
  const descId = useId()
  const panelRef = useRef<HTMLDivElement>(null)
  const onCloseRef = useRef(onClose)
  onCloseRef.current = onClose

  useEffect(() => {
    if (!open) return
    const token = Symbol('modal')
    openStack.push(token)
    const opener = document.activeElement as HTMLElement | null

    const prevOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'

    // Move focus into the dialog.
    const panel = panelRef.current
    const target =
      initialFocusRef?.current ??
      panel?.querySelector<HTMLElement>('[data-autofocus]') ??
      getFocusable(panel?.querySelector<HTMLElement>('[data-modal-body]') ?? null)[0] ??
      getFocusable(panel)[0] ??
      panel
    target?.focus()

    const onKey = (e: globalThis.KeyboardEvent) => {
      if (e.key === 'Escape' && openStack[openStack.length - 1] === token) {
        e.stopPropagation()
        onCloseRef.current()
      }
    }
    document.addEventListener('keydown', onKey)

    return () => {
      document.removeEventListener('keydown', onKey)
      const i = openStack.indexOf(token)
      if (i >= 0) openStack.splice(i, 1)
      document.body.style.overflow = prevOverflow
      // Return focus to whatever opened the dialog.
      if (opener && document.contains(opener)) opener.focus()
    }
    // initialFocusRef is a stable ref object by contract; only `open` re-runs this.
  }, [open])

  if (!open) return null

  const trapTab = (e: KeyboardEvent<HTMLDivElement>) => {
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

  const onBackdropMouseDown = (e: MouseEvent<HTMLDivElement>) => {
    if (dismissOnBackdrop && e.target === e.currentTarget) onClose()
  }

  return createPortal(
    <div
      className="fixed inset-0 z-[70] flex items-end justify-center bg-overlay p-0 animate-ui-fade-in sm:items-center sm:p-4"
      onMouseDown={onBackdropMouseDown}
      data-surface={surface}
    >
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={description ? descId : undefined}
        tabIndex={-1}
        onKeyDown={trapTab}
        className={cx(
          'flex max-h-[calc(100dvh-1rem)] w-full flex-col rounded-t-[24px] border border-line bg-surface-pop text-fg shadow-pop outline-none animate-ui-pop-in sm:max-h-[calc(100dvh-2rem)] sm:rounded-[24px]',
          SIZE[size],
          className,
        )}
      >
        <div className="flex items-start gap-3 px-6 pb-2 pt-5">
          <div className="min-w-0 flex-1">
            <h2 id={titleId} className="text-h2 text-fg">
              {title}
            </h2>
            {description ? (
              <p id={descId} className="mt-1 text-body text-fg-muted">
                {description}
              </p>
            ) : null}
          </div>
          {hideClose ? null : (
            <IconButton label="Close dialog" icon={<X />} size="sm" onClick={onClose} className="-mr-2 -mt-1" />
          )}
        </div>
        {children ? (
          <div data-modal-body className="min-h-0 flex-1 overflow-y-auto px-6 py-3">
            {children}
          </div>
        ) : null}
        {footer ? (
          <div className="flex flex-wrap items-center justify-end gap-2 px-6 pt-3 pb-[max(1.25rem,env(safe-area-inset-bottom))]">
            {footer}
          </div>
        ) : (
          <div className="h-3" />
        )}
      </div>
    </div>,
    document.body,
  )
}
