import {
  createContext,
  forwardRef,
  useContext,
  useId,
  type InputHTMLAttributes,
  type ReactNode,
  type SelectHTMLAttributes,
  type TextareaHTMLAttributes,
} from 'react'
import { cx } from './cx'
import { ChevronDown } from './icons'

interface FieldContextValue {
  id: string
  describedBy?: string
  invalid: boolean
  required: boolean
}

const FieldContext = createContext<FieldContextValue | null>(null)

export interface FieldProps {
  label: ReactNode
  /** Helper text under the control (hidden while there is an error). */
  hint?: ReactNode
  /** Error message; marks the control `aria-invalid` and is announced (role=alert). */
  error?: ReactNode
  required?: boolean
  /** Visually hide the label but keep it for screen readers. */
  labelHidden?: boolean
  className?: string
  /** An `Input`, `Select` or `Textarea` (they read id / aria wiring from the Field). */
  children: ReactNode
}

/** Label + control + hint/error, wired with matching ids and aria attributes. */
export function Field({ label, hint, error, required = false, labelHidden, className, children }: FieldProps) {
  const id = useId()
  const hintId = hint && !error ? `${id}-hint` : undefined
  const errorId = error ? `${id}-error` : undefined
  const describedBy = [hintId, errorId].filter(Boolean).join(' ') || undefined

  return (
    <FieldContext.Provider value={{ id, describedBy, invalid: !!error, required }}>
      <div className={cx('flex flex-col gap-1.5', className)}>
        <label htmlFor={id} className={cx('text-small font-medium text-fg', labelHidden && 'sr-only')}>
          {label}
          {required ? (
            <span aria-hidden className="ml-0.5 text-fg-subtle">
              *
            </span>
          ) : null}
        </label>
        {children}
        {hintId ? (
          <p id={hintId} className="text-small text-fg-subtle">
            {hint}
          </p>
        ) : null}
        {errorId ? (
          <p id={errorId} role="alert" className="text-small font-medium text-[color:var(--bad-fg)]">
            {error}
          </p>
        ) : null}
      </div>
    </FieldContext.Provider>
  )
}

function useFieldProps(own: {
  id?: string
  'aria-describedby'?: string
  'aria-invalid'?: boolean | 'true' | 'false' | 'grammar' | 'spelling'
  required?: boolean
  invalid?: boolean
}) {
  const ctx = useContext(FieldContext)
  const invalid = own.invalid ?? (own['aria-invalid'] === true || own['aria-invalid'] === 'true' ? true : ctx?.invalid ?? false)
  return {
    id: own.id ?? ctx?.id,
    'aria-describedby': own['aria-describedby'] ?? ctx?.describedBy,
    'aria-invalid': invalid || undefined,
    required: own.required ?? ctx?.required ?? undefined,
  }
}

export interface InputProps extends Omit<InputHTMLAttributes<HTMLInputElement>, 'size'> {
  /** Leading 16px icon (e.g. a search glass). */
  leftIcon?: ReactNode
  /** Trailing slot (e.g. a clear IconButton). */
  rightSlot?: ReactNode
  /** `pill` for search fields. Default `default` (12px radius). */
  shape?: 'default' | 'pill'
  invalid?: boolean
}

export const Input = forwardRef<HTMLInputElement, InputProps>(function Input(
  { leftIcon, rightSlot, shape = 'default', invalid, className, ...rest },
  ref,
) {
  const a11y = useFieldProps({ ...rest, invalid })
  const input = (
    <input
      ref={ref}
      {...rest}
      {...a11y}
      className={cx('input-base', shape === 'pill' && 'input-pill', leftIcon && 'pl-10', rightSlot && 'pr-11', className)}
    />
  )
  if (!leftIcon && !rightSlot) return input
  return (
    <div className="relative">
      {leftIcon ? (
        <span
          aria-hidden
          className="pointer-events-none absolute left-3.5 top-1/2 flex -translate-y-1/2 text-fg-subtle [&>svg]:h-4 [&>svg]:w-4"
        >
          {leftIcon}
        </span>
      ) : null}
      {input}
      {rightSlot ? <span className="absolute right-1.5 top-1/2 flex -translate-y-1/2">{rightSlot}</span> : null}
    </div>
  )
})

export interface SelectProps extends Omit<SelectHTMLAttributes<HTMLSelectElement>, 'size'> {
  invalid?: boolean
}

/** Native select with the shared input look and a chevron. */
export const Select = forwardRef<HTMLSelectElement, SelectProps>(function Select(
  { invalid, className, children, ...rest },
  ref,
) {
  const a11y = useFieldProps({ ...rest, invalid })
  return (
    <div className="relative">
      <select
        ref={ref}
        {...rest}
        {...a11y}
        className={cx('input-base cursor-pointer appearance-none pr-10', className)}
      >
        {children}
      </select>
      <ChevronDown
        aria-hidden
        className="pointer-events-none absolute right-3.5 top-1/2 h-4 w-4 -translate-y-1/2 text-fg-subtle"
      />
    </div>
  )
})

export interface TextareaProps extends TextareaHTMLAttributes<HTMLTextAreaElement> {
  invalid?: boolean
}

export const Textarea = forwardRef<HTMLTextAreaElement, TextareaProps>(function Textarea(
  { invalid, rows = 3, className, ...rest },
  ref,
) {
  const a11y = useFieldProps({ ...rest, invalid })
  return <textarea ref={ref} rows={rows} {...rest} {...a11y} className={cx('input-base resize-y', className)} />
})
