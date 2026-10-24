import { forwardRef, type ReactNode } from 'react'
import { Button, type ButtonProps } from './Button'

export interface IconButtonProps
  extends Omit<ButtonProps, 'leftIcon' | 'rightIcon' | 'children' | 'iconOnly' | 'aria-label' | 'title'> {
  /**
   * REQUIRED accessible name. Becomes `aria-label` and the native `title`
   * tooltip, so an icon-only control is never unlabeled.
   */
  label: string
  /** The icon element, e.g. `<X />` from `ui/icons`. */
  icon: ReactNode
  /** Show the label as a native tooltip (default true). */
  tooltip?: boolean
}

/** Circular icon-only button. Default variant is `ghost`. */
export const IconButton = forwardRef<HTMLButtonElement, IconButtonProps>(function IconButton(
  { label, icon, tooltip = true, variant = 'ghost', loading, ...rest },
  ref,
) {
  if (import.meta.env?.DEV && !label?.trim()) {
    console.error('IconButton: `label` is required for accessibility.')
  }
  return (
    <Button
      ref={ref}
      variant={variant}
      iconOnly
      loading={loading}
      aria-label={label}
      title={tooltip ? label : undefined}
      {...rest}
    >
      {loading ? null : icon}
    </Button>
  )
})
