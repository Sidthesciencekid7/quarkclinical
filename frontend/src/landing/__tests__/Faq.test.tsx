import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { Faq } from '../Faq'
import { FAQS } from '../content'

const ITEMS = [
  { q: 'First question?', a: 'First answer.' },
  { q: 'Second question?', a: 'Second answer.' },
  { q: 'Third question?', a: 'Third answer.' },
]

const q = (name: string) => screen.getByRole('button', { name })

describe('Faq accordion', () => {
  it('opens the first answer by default and links each button to its region', () => {
    render(<Faq items={ITEMS} />)
    expect(q('First question?')).toHaveAttribute('aria-expanded', 'true')
    expect(q('Second question?')).toHaveAttribute('aria-expanded', 'false')

    const controls = q('First question?').getAttribute('aria-controls')!
    const region = document.getElementById(controls)!
    expect(region).toHaveAttribute('role', 'region')
    expect(region).toHaveAttribute('aria-labelledby', q('First question?').id)
    expect(region).toHaveTextContent('First answer.')
    expect(region.dataset.open).toBe('true')
  })

  it('toggles aria-expanded when a question is clicked', () => {
    render(<Faq items={ITEMS} />)

    fireEvent.click(q('Second question?'))
    expect(q('Second question?')).toHaveAttribute('aria-expanded', 'true')
    // Only one answer is open at a time.
    expect(q('First question?')).toHaveAttribute('aria-expanded', 'false')

    fireEvent.click(q('Second question?'))
    expect(q('Second question?')).toHaveAttribute('aria-expanded', 'false')
    expect(
      document.getElementById(q('Second question?').getAttribute('aria-controls')!)!.dataset.open,
    ).toBe('false')
  })

  it('can start fully collapsed', () => {
    render(<Faq items={ITEMS} initialOpen={null} />)
    for (const item of ITEMS) expect(q(item.q)).toHaveAttribute('aria-expanded', 'false')
  })

  it('renders questions as headings (button inside an h3)', () => {
    render(<Faq items={ITEMS} />)
    expect(screen.getAllByRole('heading', { level: 3 })).toHaveLength(ITEMS.length)
  })
})

describe('FAQ content', () => {
  it('includes the two chat questions with the agreed answers', () => {
    const numbers = FAQS.find(f => f.q === 'Does the assistant make up numbers?')
    expect(numbers?.a).toMatch(/deterministic engines/)
    expect(numbers?.a).toMatch(/cards you confirm/)

    const stored = FAQS.find(f => f.q === 'Where are chat conversations stored?')
    expect(stored?.a).toMatch(/organization’s workspace/)
    expect(stored?.a).toMatch(/only to the person who started/)
  })

  it('makes no certification claims', () => {
    const all = FAQS.map(f => `${f.q} ${f.a}`).join(' ').toLowerCase()
    expect(all).not.toMatch(/certified|21 cfr part 11 compliant|fda.approved/)
  })
})
