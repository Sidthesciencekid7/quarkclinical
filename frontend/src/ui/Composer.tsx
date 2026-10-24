import {
  forwardRef,
  useLayoutEffect,
  useRef,
  useState,
  type ChangeEvent,
  type KeyboardEvent,
  type MouseEvent,
  type ReactNode,
} from 'react'
import { cx, mergeRefs } from './cx'
import { ArrowUp, X } from './icons'
import { Spinner } from './Spinner'

export interface ComposerProps {
  /** Controlled value. Omit for uncontrolled (use `defaultValue`). */
  value?: string
  defaultValue?: string
  onChange?: (value: string) => void
  /** Called with the trimmed text on Enter / send click. Never called for empty text. */
  onSend: (text: string) => void
  placeholder?: string
  /** Disables typing and sending. */
  disabled?: boolean
  /**
   * A turn is streaming: Enter is ignored and the send button shows a spinner.
   * With `onStop` the button becomes a Stop control.
   */
  busy?: boolean
  onStop?: () => void
  /**
   * Force a surface. `dark` is the glass variant (white text, white send
   * button) for the landing page. Omit to inherit the surrounding surface.
   */
  surface?: 'light' | 'dark'
  /** Top row, e.g. a `ComposerTile` ("Linked protocol"). */
  contextTile?: ReactNode
  /** Footer, left: mode `PillNav`, guideline pill, ... */
  footerLeft?: ReactNode
  /** Footer, right: placed before the send button. */
  footerRight?: ReactNode
  /** Textarea rows when empty (default 1). */
  minRows?: number
  /** Rows at which the textarea stops growing and scrolls (default 8). */
  maxRows?: number
  maxLength?: number
  /** Accessible name of the send button (default "Send"). */
  sendLabel?: string
  /** Accessible name of the textarea (default "Message"). */
  textareaLabel?: string
  autoFocus?: boolean
  /** Uncontrolled only: clear after sending (default true). Controlled parents clear via `value`. */
  clearOnSend?: boolean
  className?: string
  id?: string
}

const LINE_HEIGHT = 24
const TEXTAREA_PADDING = 8

/**
 * Presentational composer shell: rounded 24px box, optional context tile,
 * auto-growing textarea (1 to 8 rows), footer slots and a round send button.
 * Enter sends, Shift+Enter inserts a newline, IME composition is respected.
 * It owns no business logic - the parent decides what "send" does.
 */
export const Composer = forwardRef<HTMLTextAreaElement, ComposerProps>(function Composer(
  {
    value,
    defaultValue = '',
    onChange,
    onSend,
    placeholder = 'Message',
    disabled = false,
    busy = false,
    onStop,
    surface,
    contextTile,
    footerLeft,
    footerRight,
    minRows = 1,
    maxRows = 8,
    maxLength,
    sendLabel = 'Send',
    textareaLabel = 'Message',
    autoFocus,
    clearOnSend = true,
    className,
    id,
  },
  ref,
) {
  const controlled = value !== undefined
  const [inner, setInner] = useState(defaultValue)
  const text = controlled ? value : inner
  const taRef = useRef<HTMLTextAreaElement | null>(null)
  const composing = useRef(false)

  // Auto-grow: measure at natural height, clamp to maxRows.
  useLayoutEffect(() => {
    const el = taRef.current
    if (!el) return
    el.style.height = 'auto'
    const max = maxRows * LINE_HEIGHT + TEXTAREA_PADDING
    const full = el.scrollHeight
    el.style.height = `${Math.min(full, max)}px`
    el.style.overflowY = full > max ? 'auto' : 'hidden'
  }, [text, maxRows, minRows])

  const empty = text.trim().length === 0
  const canStop = busy && !!onStop
  const sendDisabled = canStop ? false : disabled || busy || empty

  const submit = () => {
    const trimmed = text.trim()
    if (!trimmed || disabled || busy) return
    onSend(trimmed)
    if (!controlled && clearOnSend) setInner('')
  }

  const handleChange = (e: ChangeEvent<HTMLTextAreaElement>) => {
    if (!controlled) setInner(e.target.value)
    onChange?.(e.target.value)
  }

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key !== 'Enter' || e.shiftKey) return
    // IME-safe: ignore the Enter that confirms a composition. Safari fires
    // that keydown after compositionend with keyCode 229.
    if (e.nativeEvent.isComposing || composing.current || e.keyCode === 229) return
    e.preventDefault()
    submit()
  }

  const handleSendClick = () => {
    if (canStop) onStop?.()
    else submit()
  }

  const focusTextarea = (e: MouseEvent<HTMLDivElement>) => {
    if (e.target === e.currentTarget) taRef.current?.focus()
  }

  const sendAria = canStop ? 'Stop' : sendLabel

  return (
    <div
      className={cx('composer', className)}
      data-surface={surface}
      data-busy={busy || undefined}
      onClick={focusTextarea}
    >
      {contextTile ? <div className="flex flex-wrap items-center gap-2">{contextTile}</div> : null}

      <textarea
        ref={mergeRefs(ref, taRef)}
        id={id}
        className="composer-textarea"
        rows={minRows}
        value={text}
        onChange={handleChange}
        onKeyDown={handleKeyDown}
        onCompositionStart={() => {
          composing.current = true
        }}
        onCompositionEnd={() => {
          composing.current = false
        }}
        placeholder={placeholder}
        aria-label={textareaLabel}
        disabled={disabled}
        maxLength={maxLength}
        autoFocus={autoFocus}
      />

      <div className="flex items-center justify-between gap-2">
        <div className="flex min-w-0 flex-wrap items-center gap-2">{footerLeft}</div>
        <div className="flex flex-none items-center gap-2">
          {footerRight}
          <button
            type="button"
            className="composer-send"
            onClick={handleSendClick}
            disabled={sendDisabled}
            aria-label={sendAria}
            aria-busy={busy || undefined}
            title={sendAria}
          >
            {busy ? (
              <>
                <Spinner size={20} />
                {canStop ? (
                  <span aria-hidden className="absolute h-2 w-2 rounded-[2px] bg-current" />
                ) : null}
              </>
            ) : (
              <ArrowUp aria-hidden />
            )}
          </button>
        </div>
      </div>
    </div>
  )
})

export interface ComposerTileProps {
  icon?: ReactNode
  label: ReactNode
  /** Adds a remove (x) button. */
  onRemove?: () => void
  removeLabel?: string
  className?: string
}

/** Context tile for the composer's top row (e.g. "Linked protocol: Adalimumab BE"). */
export function ComposerTile({ icon, label, onRemove, removeLabel = 'Remove', className }: ComposerTileProps) {
  return (
    <span className={cx('composer-tile', className)}>
      {icon ? <span aria-hidden className="flex [&>svg]:h-4 [&>svg]:w-4">{icon}</span> : null}
      <span className="min-w-0 truncate">{label}</span>
      {onRemove ? (
        <button
          type="button"
          aria-label={removeLabel}
          title={removeLabel}
          onClick={onRemove}
          className="-mr-1 flex h-6 w-6 items-center justify-center rounded-full text-fg-muted hover:bg-line hover:text-fg"
        >
          <X aria-hidden className="h-3.5 w-3.5" />
        </button>
      ) : null}
    </span>
  )
}
