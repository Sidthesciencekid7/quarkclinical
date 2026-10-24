import { fireEvent, render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const h = vi.hoisted(() => ({ signup: vi.fn(), login: vi.fn() }))

vi.mock('../../lib/auth', () => ({
  useLaunch: () => ({ signup: h.signup, login: h.login }),
  authEnabled: false,
}))
vi.mock('../../api/client', () => ({ api: { submitLead: vi.fn() } }))

import Landing from '../../pages/Landing'

const renderLanding = () =>
  render(
    <MemoryRouter>
      <Landing />
    </MemoryRouter>,
  )

describe('Landing page', () => {
  const originalScrollIntoView = Element.prototype.scrollIntoView
  beforeEach(() => {
    h.signup.mockClear()
    h.login.mockClear()
    Element.prototype.scrollIntoView = vi.fn()
    localStorage.clear()
  })
  afterEach(() => {
    Element.prototype.scrollIntoView = originalScrollIntoView
    document.body.style.overflow = ''
  })

  it('has a skip link, one h1 and the four main nav links', () => {
    renderLanding()
    expect(screen.getByRole('link', { name: 'Skip to main content' })).toHaveAttribute('href', '#main')
    expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1)
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent(
      'Study protocols and budgets, built on computed evidence.',
    )

    const nav = screen.getByRole('navigation', { name: 'Main' })
    const hrefs = within(nav)
      .getAllByRole('link')
      .map(a => a.getAttribute('href'))
    expect(hrefs).toEqual(['#products', '#how', '#pricing', '#faq'])
    // Every anchor target exists.
    for (const href of hrefs) expect(document.getElementById(href!.slice(1))).not.toBeNull()
  })

  it('renders every section in order', () => {
    renderLanding()
    const ids = ['top', 'products', 'chat', 'why', 'how', 'pricing', 'faq', 'cta']
    const positions = ids.map(id => {
      const el = document.getElementById(id)
      expect(el, `#${id}`).not.toBeNull()
      return el!
    })
    for (let i = 1; i < positions.length; i++) {
      expect(positions[i - 1].compareDocumentPosition(positions[i]) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    }
  })

  it('shows three tiers, all "Price on request", and no certification or testimonial claims', () => {
    renderLanding()
    expect(screen.getAllByText('Price on request')).toHaveLength(3)
    const text = document.body.textContent!.toLowerCase()
    expect(text).not.toMatch(/certified|testimonial/)
    expect(screen.getByText(/illustration, example values/i)).toBeInTheDocument()
  })

  it('opens the contact modal from the pricing tiers with the matching interest pre-selected', () => {
    renderLanding()
    fireEvent.click(screen.getByRole('button', { name: /Request a pilot: Pilot plan/ }))
    expect(screen.getByRole('button', { name: 'Request a pilot on a real study' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByRole('dialog')).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: /Contact us: Team plan/ }))
    expect(screen.getByRole('button', { name: 'Pricing and plans' })).toHaveAttribute('aria-pressed', 'true')
  })

  it('opens "Book a demo", closes on Escape and returns focus to the button that opened it', () => {
    renderLanding()
    const heroDemo = within(document.getElementById('top')!).getByRole('button', { name: 'Book a demo' })
    heroDemo.focus()
    fireEvent.click(heroDemo)

    expect(screen.getByRole('dialog', { name: 'Book a demo' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Book a demo', pressed: true })).toBeInTheDocument()

    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(document.activeElement).toBe(heroDemo)
  })

  it('"Log in" in the nav calls the launch login', () => {
    renderLanding()
    const header = document.querySelector('header')!
    fireEvent.click(within(header).getAllByRole('button', { name: 'Log in' })[0])
    expect(h.login).toHaveBeenCalledTimes(1)
  })

  it('"Start with your study" scrolls to the hero composer and focuses it', () => {
    renderLanding()
    fireEvent.click(screen.getByRole('button', { name: 'Start with your study' }))
    const box = screen.getByRole('textbox', { name: 'Describe your study' })
    expect(Element.prototype.scrollIntoView).toHaveBeenCalled()
    expect(document.activeElement).toBe(box)
  })

  it('opens and closes the mobile menu sheet with Escape, returning focus to its button', () => {
    renderLanding()
    const toggle = screen.getByRole('button', { name: 'Open menu' })
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    toggle.focus()
    fireEvent.click(toggle)

    const open = screen.getByRole('button', { name: 'Close menu' })
    expect(open).toHaveAttribute('aria-expanded', 'true')
    const sheet = document.getElementById('lp-mobile-menu')!
    expect(within(sheet).getAllByRole('link').map(a => a.textContent)).toEqual([
      'Products',
      'How it works',
      'Pricing',
      'FAQ',
    ])

    fireEvent.keyDown(document, { key: 'Escape' })
    expect(document.getElementById('lp-mobile-menu')).toBeNull()
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Open menu' }))
  })
})
