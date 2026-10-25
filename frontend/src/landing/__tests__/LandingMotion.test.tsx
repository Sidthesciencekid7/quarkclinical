import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const h = vi.hoisted(() => {
  const engine = {
    scrollTo: vi.fn(() => true),
    setLocked: vi.fn(),
    destroy: vi.fn(),
  }
  return { engine, init: vi.fn() }
})

vi.mock('../../lib/auth', () => ({
  useLaunch: () => ({ signup: vi.fn(), login: vi.fn() }),
  authEnabled: false,
}))
vi.mock('../../api/client', () => ({ api: { submitLead: vi.fn() } }))
// The real engine (gsap + lenis) needs a layout engine; the hook's contract is
// what is under test here.
vi.mock('../motion/engine', () => ({ initLandingMotion: h.init }))

import Landing from '../../pages/Landing'

function stubMatchMedia(reduced = false) {
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: query.includes('prefers-reduced-motion: reduce') ? reduced : false,
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  }))
}

const renderLanding = () =>
  render(
    <MemoryRouter>
      <Landing />
    </MemoryRouter>,
  )

const root = () => document.querySelector('.lp-root') as HTMLElement

describe('useLandingMotion (through the landing page)', () => {
  beforeEach(() => {
    stubMatchMedia(false)
    h.engine.scrollTo.mockClear().mockReturnValue(true)
    h.engine.setLocked.mockClear()
    h.engine.destroy.mockClear()
    h.init.mockReset().mockReturnValue(h.engine)
    localStorage.clear()
  })
  afterEach(() => {
    vi.unstubAllGlobals()
    window.history.replaceState({}, '', '/')
    document.body.style.overflow = ''
  })

  it('claims the hidden-until-animated state, starts the engine on the root, and destroys it on unmount', async () => {
    const { unmount } = renderLanding()
    expect(root().classList.contains('lp-motion')).toBe(true)

    await waitFor(() => expect(h.init).toHaveBeenCalledTimes(1))
    expect(h.init).toHaveBeenCalledWith(root())

    unmount()
    expect(h.engine.destroy).toHaveBeenCalledTimes(1)
  })

  it('falls back to the static page when the engine throws while starting', async () => {
    const err = vi.spyOn(console, 'error').mockImplementation(() => {})
    h.init.mockImplementation(() => {
      throw new Error('boom')
    })
    renderLanding()
    await waitFor(() => expect(root().classList.contains('lp-motion')).toBe(false))
    // Everything is still on the page as plain text.
    expect(screen.getByRole('heading', { level: 3, name: 'Engines do the math.' })).toBeVisible()
    err.mockRestore()
  })

  it('does not start with prefers-reduced-motion, and leaves the page static', () => {
    stubMatchMedia(true)
    renderLanding()
    expect(root().classList.contains('lp-motion')).toBe(false)
    expect(h.init).not.toHaveBeenCalled()
  })

  it('does not start when ?motion=off is in the URL', () => {
    window.history.replaceState({}, '', '/?motion=off')
    renderLanding()
    expect(root().classList.contains('lp-motion')).toBe(false)
    expect(h.init).not.toHaveBeenCalled()
  })

  it('pauses smooth scrolling while the demo dialog is open and resumes when it closes', async () => {
    renderLanding()
    await waitFor(() => expect(h.init).toHaveBeenCalled())

    fireEvent.click(screen.getAllByRole('button', { name: 'Book a demo' })[0])
    expect(screen.getByRole('dialog', { name: 'Book a demo' })).toBeInTheDocument()
    await waitFor(() => expect(h.engine.setLocked).toHaveBeenLastCalledWith(true))

    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(h.engine.setLocked).toHaveBeenLastCalledWith(false))
  })

  it('pauses smooth scrolling while the mobile menu is open', async () => {
    renderLanding()
    await waitFor(() => expect(h.init).toHaveBeenCalled())

    fireEvent.click(screen.getByRole('button', { name: 'Open menu' }))
    await waitFor(() => expect(h.engine.setLocked).toHaveBeenLastCalledWith(true))
    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(h.engine.setLocked).toHaveBeenLastCalledWith(false))
  })

  it('"Start with your study" scrolls through the engine (centered on the composer) and focuses it', async () => {
    const scrollIntoView = vi.fn()
    const original = Element.prototype.scrollIntoView
    Element.prototype.scrollIntoView = scrollIntoView
    try {
      renderLanding()
      await waitFor(() => expect(h.init).toHaveBeenCalled())

      fireEvent.click(screen.getByRole('button', { name: 'Start with your study' }))
      const box = screen.getByRole('textbox', { name: 'Describe your study' })
      expect(h.engine.scrollTo).toHaveBeenCalledWith(box, { center: true })
      expect(scrollIntoView).not.toHaveBeenCalled()
      expect(document.activeElement).toBe(box)
    } finally {
      Element.prototype.scrollIntoView = original
    }
  })
})
