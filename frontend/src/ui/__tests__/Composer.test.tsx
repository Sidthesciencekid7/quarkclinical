import { act, fireEvent, render, screen } from '@testing-library/react'
import type { ComponentProps } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { Composer } from '../Composer'

function setup(props: Partial<ComponentProps<typeof Composer>> = {}) {
  const onSend = vi.fn()
  render(<Composer onSend={onSend} {...props} />)
  const box = screen.getByRole('textbox', { name: 'Message' })
  const send = screen.getByRole('button', { name: 'Send' })
  return { onSend, box, send }
}

describe('Composer', () => {
  it('disables send while the box is empty or whitespace', () => {
    const { box, send } = setup()
    expect(send).toBeDisabled()
    fireEvent.change(box, { target: { value: '   ' } })
    expect(send).toBeDisabled()
    fireEvent.change(box, { target: { value: 'hello' } })
    expect(send).toBeEnabled()
  })

  it('sends on Enter and clears the box', () => {
    const { box, onSend } = setup()
    fireEvent.change(box, { target: { value: 'Adalimumab biosimilar PK' } })
    fireEvent.keyDown(box, { key: 'Enter' })
    expect(onSend).toHaveBeenCalledTimes(1)
    expect(onSend).toHaveBeenCalledWith('Adalimumab biosimilar PK')
    expect(box).toHaveValue('')
  })

  it('does not send on Shift+Enter (newline instead)', () => {
    const { box, onSend } = setup()
    fireEvent.change(box, { target: { value: 'line one' } })
    // fireEvent returns false when the default action was cancelled.
    const notPrevented = fireEvent.keyDown(box, { key: 'Enter', shiftKey: true })
    expect(onSend).not.toHaveBeenCalled()
    expect(notPrevented).toBe(true)
    expect(box).toHaveValue('line one')
  })

  it('does not send on Enter while empty', () => {
    const { box, onSend } = setup()
    fireEvent.keyDown(box, { key: 'Enter' })
    expect(onSend).not.toHaveBeenCalled()
  })

  it('ignores the Enter that confirms an IME composition', () => {
    const { box, onSend } = setup()
    fireEvent.change(box, { target: { value: 'nihon' } })
    fireEvent.keyDown(box, { key: 'Enter', isComposing: true })
    fireEvent.keyDown(box, { key: 'Enter', keyCode: 229 }) // Safari: keydown after compositionend
    fireEvent.compositionStart(box)
    fireEvent.keyDown(box, { key: 'Enter' })
    expect(onSend).not.toHaveBeenCalled()
    fireEvent.compositionEnd(box)
    fireEvent.keyDown(box, { key: 'Enter' })
    expect(onSend).toHaveBeenCalledTimes(1)
  })

  it('sends via the send button', () => {
    const { box, send, onSend } = setup()
    fireEvent.change(box, { target: { value: 'go' } })
    fireEvent.click(send)
    expect(onSend).toHaveBeenCalledWith('go')
  })

  it('works controlled: the parent owns the value', () => {
    const onSend = vi.fn()
    const onChange = vi.fn()
    const { rerender } = render(<Composer value="draft" onChange={onChange} onSend={onSend} />)
    const box = screen.getByRole('textbox', { name: 'Message' })
    fireEvent.keyDown(box, { key: 'Enter' })
    expect(onSend).toHaveBeenCalledWith('draft')
    expect(box).toHaveValue('draft') // not cleared by the component
    fireEvent.change(box, { target: { value: 'draft 2' } })
    expect(onChange).toHaveBeenCalledWith('draft 2')
    rerender(<Composer value="" onChange={onChange} onSend={onSend} />)
    expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled()
  })

  it('ignores Enter and send while busy; busy + onStop turns send into Stop', () => {
    const onSend = vi.fn()
    const onStop = vi.fn()
    const { rerender } = render(<Composer value="hi" onSend={onSend} busy />)
    fireEvent.keyDown(screen.getByRole('textbox'), { key: 'Enter' })
    expect(onSend).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Send' })).toHaveAttribute('aria-busy', 'true')

    rerender(<Composer value="hi" onSend={onSend} busy onStop={onStop} />)
    const stop = screen.getByRole('button', { name: 'Stop' })
    expect(stop).toBeEnabled()
    fireEvent.click(stop)
    expect(onStop).toHaveBeenCalledTimes(1)
  })

  it('applies the dark glass surface and renders footer + context slots', () => {
    const { container } = render(
      <Composer
        onSend={() => {}}
        surface="dark"
        contextTile={<span>Linked protocol</span>}
        footerLeft={<span>Protocol mode</span>}
      />,
    )
    expect(container.querySelector('.composer')).toHaveAttribute('data-surface', 'dark')
    expect(screen.getByText('Linked protocol')).toBeInTheDocument()
    expect(screen.getByText('Protocol mode')).toBeInTheDocument()
  })

  describe('auto-grow re-measure on resize', () => {
    type RoCallback = (entries: { contentRect: { width: number } }[]) => void
    let roCallback: RoCallback | null = null
    let disconnected = false
    // scrollHeight of the (empty) box: 24 when the placeholder fits on one line,
    // 72 when a narrow width wraps it onto three.
    let naturalHeight = 24

    beforeEach(() => {
      roCallback = null
      disconnected = false
      naturalHeight = 24
      Object.defineProperty(HTMLTextAreaElement.prototype, 'scrollHeight', {
        configurable: true,
        get: () => naturalHeight,
      })
      vi.stubGlobal(
        'ResizeObserver',
        class {
          constructor(cb: RoCallback) {
            roCallback = cb
          }
          observe() {}
          unobserve() {}
          disconnect() {
            disconnected = true
          }
        },
      )
    })

    afterEach(() => {
      vi.unstubAllGlobals()
      // Drop the shadowing getter so jsdom's own scrollHeight is visible again.
      delete (HTMLTextAreaElement.prototype as unknown as Record<string, unknown>).scrollHeight
    })

    it('re-measures an empty box when its width changes, without a keystroke', () => {
      const { box } = setup()
      expect(box.style.height).toBe('24px')

      // The viewport narrows: the placeholder wraps, so the natural height grows.
      naturalHeight = 72
      act(() => roCallback?.([{ contentRect: { width: 280 } }]))
      expect(box.style.height).toBe('72px')

      // And back: the box shrinks again.
      naturalHeight = 24
      act(() => roCallback?.([{ contentRect: { width: 640 } }]))
      expect(box.style.height).toBe('24px')
    })

    it('ignores callbacks where the width did not change (its own height writes must not loop)', () => {
      const { box } = setup()
      act(() => roCallback?.([{ contentRect: { width: 500 } }]))
      naturalHeight = 96
      act(() => roCallback?.([{ contentRect: { width: 500 } }]))
      expect(box.style.height).toBe('24px')
    })

    it('stops observing on unmount', () => {
      const { unmount } = render(<Composer onSend={() => {}} />)
      unmount()
      expect(disconnected).toBe(true)
    })
  })

  it('renders fine where ResizeObserver does not exist', () => {
    vi.stubGlobal('ResizeObserver', undefined)
    try {
      const { box } = setup()
      expect(box).toBeInTheDocument()
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it('a disabled composer cannot send', () => {
    const { box, onSend, send } = setup({ disabled: true, defaultValue: 'x' })
    expect(box).toBeDisabled()
    expect(send).toBeDisabled()
    fireEvent.keyDown(box, { key: 'Enter' })
    expect(onSend).not.toHaveBeenCalled()
  })
})
