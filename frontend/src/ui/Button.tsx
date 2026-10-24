import {
  forwardRef,
  useLayoutEffect,
  useRef,
  type ButtonHTMLAttributes,
  type ComponentPropsWithoutRef,
  type ElementType,
  type MouseEvent,
  type ReactElement,
  type ReactNode,
  type Ref,
} from 'react'
import { cx, mergeRefs } from './cx'
import { Spinner } from './Spinner'

export type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger' | 'accent' | 'link'
export type ButtonSize = 'sm' | 'md' | 'lg' | 'xl'

// Full literal class names so Tailwind's content scan keeps them in the build.
const VARIANT_CLASS: Record<ButtonVariant, string> = {
  primary: 'btn-primary',
  secondary: 'btn-secondary',
  ghost: 'btn-ghost',
  danger: 'btn-danger',
  accent: 'btn-accent',
  link: 'btn-link',
}
const SIZE_CLASS: Record<ButtonSize, string> = {
  sm: 'btn-sm',
  md: 'btn-md',
  lg: 'btn-lg',
  xl: 'btn-xl',
}

export interface ButtonStyleOptions {
  variant?: ButtonVariant
  size?: ButtonSize
  /** Circle of the size's height (used by IconButton). */
  iconOnly?: boolean
  /** `danger` only: filled red. Reserve for destructive confirm dialogs. */
  solid?: boolean
  fullWidth?: boolean
  className?: string
}

/** The class string for the pill button family. Use on any element (e.g. a router `<Link>`). */
export function buttonClasses({
  variant = 'primary',
  size = 'md',
  iconOnly,
  solid,
  fullWidth,
  className,
}: ButtonStyleOptions = {}): string {
  return cx(
    'btn',
    SIZE_CLASS[size],
    VARIANT_CLASS[variant],
    variant === 'danger' && solid && 'btn-danger-solid',
    iconOnly && 'btn-icon',
    fullWidth && 'w-full',
    className,
  )
}

interface ButtonVisualProps {
  /** Default `primary` (black on light surfaces, white on dark). */
  variant?: ButtonVariant
  /** Default `md`. */
  size?: ButtonSize
  leftIcon?: ReactNode
  rightIcon?: ReactNode
  solid?: boolean
  fullWidth?: boolean
}

export interface ButtonProps
  extends ButtonVisualProps,
    Omit<ButtonHTMLAttributes<HTMLButtonElement>, 'color'> {
  /**
   * Swaps the leading icon for a spinner, sets `aria-busy`, disables the button
   * and keeps its pre-loading width so layout does not jump.
   */
  loading?: boolean
  /** Internal: IconButton renders a circle. Prefer `<IconButton>`. */
  iconOnly?: boolean
}

/**
 * Pill button. `type` defaults to "button" (not "submit") so a Button inside a
 * form never submits by accident - pass `type="submit"` where you mean it.
 * Disabled and loading buttons are natively `disabled` AND `aria-disabled`;
 * their click handlers never run.
 */
export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  {
    variant = 'primary',
    size = 'md',
    leftIcon,
    rightIcon,
    loading = false,
    disabled = false,
    solid,
    fullWidth,
    iconOnly,
    type = 'button',
    className,
    style,
    onClick,
    children,
    ...rest
  },
  ref,
) {
  const innerRef = useRef<HTMLButtonElement | null>(null)
  const widthRef = useRef<number | undefined>(undefined)

  // Remember the settled (non-loading) width so the loading state can hold it.
  useLayoutEffect(() => {
    if (!loading && innerRef.current) {
      const w = innerRef.current.offsetWidth
      if (w > 0) widthRef.current = w
    }
  })

  const inert = disabled || loading
  const lockWidth = loading && variant !== 'link' && widthRef.current
  const lockedStyle = lockWidth ? { width: widthRef.current, ...style } : style

  const handleClick = (e: MouseEvent<HTMLButtonElement>) => {
    if (inert) {
      e.preventDefault()
      return
    }
    onClick?.(e)
  }

  const leading = loading ? <Spinner /> : iconOnly ? null : leftIcon

  return (
    <button
      ref={mergeRefs(ref, innerRef)}
      type={type}
      className={buttonClasses({ variant, size, iconOnly, solid, fullWidth, className })}
      style={lockedStyle}
      disabled={inert}
      aria-disabled={inert || undefined}
      aria-busy={loading || undefined}
      onClick={handleClick}
      {...rest}
    >
      {leading}
      {children}
      {iconOnly ? null : rightIcon}
    </button>
  )
})

/* ───────────────────────── ButtonLink ───────────────────────── */

interface ButtonLinkOwnProps extends ButtonVisualProps {
  /** Rendered as aria-disabled with tabIndex -1; navigation is blocked. */
  disabled?: boolean
  className?: string
  children?: ReactNode
  onClick?: (e: MouseEvent<HTMLElement>) => void
}

export type ButtonLinkProps<C extends ElementType = 'a'> = ButtonLinkOwnProps & {
  /** Element or component to render, e.g. react-router's `Link`. Default `a`. */
  as?: C
} & Omit<ComponentPropsWithoutRef<C>, keyof ButtonLinkOwnProps | 'as'>

function ButtonLinkInner<C extends ElementType = 'a'>(
  props: ButtonLinkProps<C>,
  ref: Ref<Element>,
) {
  const {
    as,
    variant = 'primary',
    size = 'md',
    leftIcon,
    rightIcon,
    solid,
    fullWidth,
    disabled,
    className,
    onClick,
    children,
    ...rest
  } = props as ButtonLinkOwnProps & { as?: ElementType } & Record<string, unknown>
  const Comp: ElementType = as ?? 'a'

  const handleClick = (e: MouseEvent<HTMLElement>) => {
    if (disabled) {
      e.preventDefault()
      return
    }
    onClick?.(e)
  }

  return (
    <Comp
      ref={ref}
      className={buttonClasses({ variant, size, solid, fullWidth, className })}
      aria-disabled={disabled || undefined}
      tabIndex={disabled ? -1 : undefined}
      onClick={handleClick}
      {...rest}
    >
      {leftIcon}
      {children}
      {rightIcon}
    </Comp>
  )
}

/** Anchor (or router Link via `as`) that looks like a Button. */
export const ButtonLink = forwardRef(ButtonLinkInner as never) as unknown as (<
  C extends ElementType = 'a',
>(
  props: ButtonLinkProps<C> & { ref?: Ref<Element> },
) => ReactElement | null) & { displayName?: string }
ButtonLink.displayName = 'ButtonLink'
