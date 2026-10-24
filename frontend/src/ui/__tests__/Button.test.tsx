import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { Button, ButtonLink, buttonClasses } from '../Button'
import { Plus } from '../icons'

describe('Button', () => {
  it('shows an aria-busy spinner and is disabled while loading', () => {
    const onClick = vi.fn()
    render(
      <Button loading onClick={onClick}>
        Save
      </Button>,
    )
    const btn = screen.getByRole('button', { name: 'Save' })
    expect(btn).toHaveAttribute('aria-busy', 'true')
    expect(btn).toBeDisabled()
    expect(btn).toHaveAttribute('aria-disabled', 'true')
    expect(btn.querySelector('.ui-spinner')).toBeInTheDocument()
    fireEvent.click(btn)
    expect(onClick).not.toHaveBeenCalled()
  })

  it('swaps the leading icon for the spinner while loading (and back after)', () => {
    const { rerender } = render(
      <Button leftIcon={<Plus data-testid="lead" />} loading>
        Add
      </Button>,
    )
    expect(screen.queryByTestId('lead')).not.toBeInTheDocument()
    expect(screen.getByRole('button').querySelectorAll('.ui-spinner')).toHaveLength(1)

    rerender(<Button leftIcon={<Plus data-testid="lead" />}>Add</Button>)
    const btn = screen.getByRole('button', { name: 'Add' })
    expect(screen.getByTestId('lead')).toBeInTheDocument()
    expect(btn.querySelector('.ui-spinner')).not.toBeInTheDocument()
    expect(btn).not.toHaveAttribute('aria-busy')
    expect(btn).toBeEnabled()
  })

  it('marks disabled buttons aria-disabled and never fires onClick', () => {
    const onClick = vi.fn()
    render(
      <Button disabled onClick={onClick}>
        Nope
      </Button>,
    )
    const btn = screen.getByRole('button', { name: 'Nope' })
    expect(btn).toBeDisabled()
    expect(btn).toHaveAttribute('aria-disabled', 'true')
    fireEvent.click(btn)
    expect(onClick).not.toHaveBeenCalled()
  })

  it('fires onClick and defaults to type="button"', () => {
    const onClick = vi.fn()
    render(<Button onClick={onClick}>Go</Button>)
    const btn = screen.getByRole('button', { name: 'Go' })
    expect(btn).toHaveAttribute('type', 'button')
    fireEvent.click(btn)
    expect(onClick).toHaveBeenCalledTimes(1)
  })

  it('applies variant and size classes', () => {
    render(
      <Button variant="secondary" size="lg">
        Pill
      </Button>,
    )
    const btn = screen.getByRole('button', { name: 'Pill' })
    expect(btn).toHaveClass('btn', 'btn-lg', 'btn-secondary')
  })

  it('buttonClasses builds the same class string for non-button elements', () => {
    expect(buttonClasses({ variant: 'danger', size: 'sm', solid: true })).toBe(
      'btn btn-sm btn-danger btn-danger-solid',
    )
  })
})

describe('ButtonLink', () => {
  it('renders an anchor with button classes', () => {
    render(
      <ButtonLink href="/x" variant="secondary">
        Docs
      </ButtonLink>,
    )
    const a = screen.getByRole('link', { name: 'Docs' })
    expect(a).toHaveAttribute('href', '/x')
    expect(a).toHaveClass('btn', 'btn-secondary')
  })

  it('blocks clicks and removes the tab stop when disabled', () => {
    const onClick = vi.fn()
    render(
      <ButtonLink href="/x" disabled onClick={onClick}>
        Off
      </ButtonLink>,
    )
    const a = screen.getByRole('link', { name: 'Off' })
    expect(a).toHaveAttribute('aria-disabled', 'true')
    expect(a).toHaveAttribute('tabindex', '-1')
    fireEvent.click(a)
    expect(onClick).not.toHaveBeenCalled()
  })
})
