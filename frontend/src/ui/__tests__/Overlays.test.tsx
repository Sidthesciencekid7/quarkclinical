import { fireEvent, render, screen } from '@testing-library/react'
import { useState } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { Button } from '../Button'
import { Menu } from '../Menu'
import { Modal } from '../Modal'

function ModalHarness({ onClose }: { onClose?: () => void }) {
  const [open, setOpen] = useState(false)
  return (
    <>
      <Button onClick={() => setOpen(true)}>Open</Button>
      <Modal
        open={open}
        onClose={() => {
          onClose?.()
          setOpen(false)
        }}
        title="Contact sales"
        footer={<Button>Submit</Button>}
      >
        <input aria-label="Email" />
      </Modal>
    </>
  )
}

describe('Modal', () => {
  it('renders an aria-modal dialog named by its title, with focus inside', () => {
    render(<ModalHarness />)
    fireEvent.click(screen.getByRole('button', { name: 'Open' }))
    const dialog = screen.getByRole('dialog', { name: 'Contact sales' })
    expect(dialog).toHaveAttribute('aria-modal', 'true')
    expect(dialog.contains(document.activeElement)).toBe(true)
  })

  it('closes on Escape and returns focus to the opener', () => {
    const onClose = vi.fn()
    render(<ModalHarness onClose={onClose} />)
    const opener = screen.getByRole('button', { name: 'Open' })
    opener.focus()
    fireEvent.click(opener)
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(opener).toHaveFocus()
  })

  it('traps Tab inside the dialog', () => {
    render(<ModalHarness />)
    fireEvent.click(screen.getByRole('button', { name: 'Open' }))
    const dialog = screen.getByRole('dialog')
    const submit = screen.getByRole('button', { name: 'Submit' })
    submit.focus()
    fireEvent.keyDown(dialog, { key: 'Tab' }) // last -> wraps to first
    expect(dialog.contains(document.activeElement)).toBe(true)
    expect(document.activeElement).not.toBe(submit)
    fireEvent.keyDown(dialog, { key: 'Tab', shiftKey: true }) // first -> wraps to last
    expect(document.activeElement).toBe(submit)
  })

  it('closes when the backdrop is pressed but not when the panel is', () => {
    const onClose = vi.fn()
    render(<ModalHarness onClose={onClose} />)
    fireEvent.click(screen.getByRole('button', { name: 'Open' }))
    fireEvent.mouseDown(screen.getByRole('dialog'))
    expect(onClose).not.toHaveBeenCalled()
    fireEvent.mouseDown(screen.getByRole('dialog').parentElement as HTMLElement)
    expect(onClose).toHaveBeenCalledTimes(1)
  })
})

describe('Menu', () => {
  const setup = () => {
    const onTeam = vi.fn()
    const onSignOut = vi.fn()
    render(
      <Menu
        label="Workspace"
        trigger={<Button variant="secondary">Acme Bio</Button>}
        items={[
          { id: 'team', label: 'Team & seats', onSelect: onTeam },
          { id: 'sep', type: 'separator' },
          { id: 'out', label: 'Sign out', danger: true, onSelect: onSignOut },
        ]}
      />,
    )
    return { onTeam, onSignOut, trigger: screen.getByRole('button', { name: 'Acme Bio' }) }
  }

  it('opens on click with the first item focused; aria wiring is set', () => {
    const { trigger } = setup()
    expect(trigger).toHaveAttribute('aria-haspopup', 'menu')
    expect(trigger).toHaveAttribute('aria-expanded', 'false')
    fireEvent.click(trigger)
    expect(trigger).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByRole('menu', { name: 'Workspace' })).toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: 'Team & seats' })).toHaveFocus()
  })

  it('moves with arrow keys (wrapping) and Home/End', () => {
    const { trigger } = setup()
    fireEvent.click(trigger)
    const menu = screen.getByRole('menu')
    fireEvent.keyDown(menu, { key: 'ArrowDown' })
    expect(screen.getByRole('menuitem', { name: 'Sign out' })).toHaveFocus()
    fireEvent.keyDown(menu, { key: 'ArrowDown' })
    expect(screen.getByRole('menuitem', { name: 'Team & seats' })).toHaveFocus()
    fireEvent.keyDown(menu, { key: 'End' })
    expect(screen.getByRole('menuitem', { name: 'Sign out' })).toHaveFocus()
    fireEvent.keyDown(menu, { key: 'Home' })
    expect(screen.getByRole('menuitem', { name: 'Team & seats' })).toHaveFocus()
  })

  it('opens from the keyboard with ArrowDown on the trigger', () => {
    const { trigger } = setup()
    fireEvent.keyDown(trigger, { key: 'ArrowDown' })
    expect(screen.getByRole('menu')).toBeInTheDocument()
  })

  it('closes on Escape and returns focus to the trigger', () => {
    const { trigger } = setup()
    fireEvent.click(trigger)
    fireEvent.keyDown(screen.getByRole('menu'), { key: 'Escape' })
    expect(screen.queryByRole('menu')).not.toBeInTheDocument()
    expect(trigger).toHaveFocus()
    expect(trigger).toHaveAttribute('aria-expanded', 'false')
  })

  it('runs onSelect, closes, and returns focus to the trigger', () => {
    const { trigger, onSignOut } = setup()
    fireEvent.click(trigger)
    fireEvent.click(screen.getByRole('menuitem', { name: 'Sign out' }))
    expect(onSignOut).toHaveBeenCalledTimes(1)
    expect(screen.queryByRole('menu')).not.toBeInTheDocument()
    expect(trigger).toHaveFocus()
  })

  it('closes when clicking outside', () => {
    const { trigger } = setup()
    fireEvent.click(trigger)
    fireEvent.mouseDown(document.body)
    expect(screen.queryByRole('menu')).not.toBeInTheDocument()
  })
})
