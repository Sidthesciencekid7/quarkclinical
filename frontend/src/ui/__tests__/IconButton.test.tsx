import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { IconButton } from '../IconButton'
import { X } from '../icons'

describe('IconButton', () => {
  it('exposes its label as the accessible name and tooltip', () => {
    render(<IconButton label="Close panel" icon={<X />} />)
    const btn = screen.getByRole('button', { name: 'Close panel' })
    expect(btn).toHaveAttribute('aria-label', 'Close panel')
    expect(btn).toHaveAttribute('title', 'Close panel')
    expect(btn).toHaveClass('btn', 'btn-icon')
  })

  it('requires a label at the type level', () => {
    // @ts-expect-error `label` is required - an unlabeled icon button must not compile.
    const bad = <IconButton icon={<X />} />
    expect(bad).toBeTruthy()
  })

  it('calls onClick, and shows a single spinner while loading', () => {
    const onClick = vi.fn()
    const { rerender } = render(<IconButton label="Refresh" icon={<X />} onClick={onClick} />)
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    expect(onClick).toHaveBeenCalledTimes(1)

    rerender(<IconButton label="Refresh" icon={<X />} loading onClick={onClick} />)
    const btn = screen.getByRole('button', { name: 'Refresh' })
    expect(btn).toHaveAttribute('aria-busy', 'true')
    expect(btn.querySelectorAll('.ui-spinner')).toHaveLength(1)
  })
})
