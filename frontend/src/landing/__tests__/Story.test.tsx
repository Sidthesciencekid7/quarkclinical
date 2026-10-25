import { render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const h = vi.hoisted(() => ({ init: vi.fn() }))

vi.mock('../../lib/auth', () => ({
  useLaunch: () => ({ signup: vi.fn(), login: vi.fn() }),
  authEnabled: false,
}))
vi.mock('../../api/client', () => ({ api: { submitLead: vi.fn() } }))
vi.mock('../motion/engine', () => ({ initLandingMotion: h.init }))

import Landing from '../../pages/Landing'
import { Story } from '../Story'
import {
  BUDGET_LINES,
  BUDGET_TOTAL,
  METRICS,
  PROTOCOL_SECTIONS,
  STORY_NOTE,
  STORY_PROMPT,
  STORY_STAGES,
} from '../content'

/** A matchMedia that reports prefers-reduced-motion as `reduced` and nothing else. */
function stubMatchMedia(reduced: boolean) {
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

describe('the pinned story, reduced motion (every stage is plain visible text)', () => {
  beforeEach(() => {
    stubMatchMedia(true)
    h.init.mockReset()
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('renders all five stages with a visible headline and body, nothing hidden', () => {
    render(
      <MemoryRouter>
        <Landing />
      </MemoryRouter>,
    )
    const root = document.querySelector('.lp-root')!
    // No motion chunk, no hidden-until-animated state, no engine.
    expect(root.classList.contains('lp-motion')).toBe(false)
    expect(h.init).not.toHaveBeenCalled()

    const story = document.getElementById('chat')!
    const headings = within(story).getAllByRole('heading', { level: 3 })
    expect(headings.map(x => x.textContent)).toEqual(STORY_STAGES.map(s => s.title))
    for (const stage of STORY_STAGES) {
      expect(screen.getByRole('heading', { level: 3, name: stage.title })).toBeVisible()
      expect(within(story).getByText(stage.body)).toBeVisible()
    }
  })

  it('keeps every panel as real text: typed prompt, locked numbers, 17 sections and the budget', () => {
    render(<Story />)
    const story = document.getElementById('chat')!

    // Stage 1: the full sentence is in the DOM (the typing animation only rewrites a visual copy).
    expect(within(story).getAllByText(STORY_PROMPT).length).toBeGreaterThan(0)
    // Stage 2: the drug match and the study-type gate.
    expect(within(story).getByText('adalimumab')).toBeVisible()
    expect(within(story).getByText('Biosimilar PK')).toBeVisible()
    expect(within(story).getAllByText('Not applicable to biologics')).toHaveLength(2)
    // Stage 3: engine rows with their final numbers.
    expect(within(story).getByText('Sample size')).toBeVisible()
    expect(story.querySelector('.p3-num[data-to="32"]')?.textContent).toBe('32')
    // Stage 4: all 17 real section titles.
    for (const s of PROTOCOL_SECTIONS) expect(within(story).getByText(s.title)).toBeInTheDocument()
    expect(story.querySelectorAll('.p4-row')).toHaveLength(17)
    // Stage 5: the total and every line.
    expect(story.querySelector('.p5-total')?.textContent).toBe(BUDGET_TOTAL.toLocaleString('en-US'))
    for (const l of BUDGET_LINES) expect(within(story).getByText(l.label)).toBeVisible()
  })

  it('labels the whole story as an illustration with example values, exactly once', () => {
    render(<Story />)
    expect(screen.getAllByText(STORY_NOTE)).toHaveLength(1)
  })

  it('hides the decorative chrome (progress pill and frame) from assistive tech', () => {
    render(<Story />)
    expect(document.querySelector('.lp-story__prog')).toHaveAttribute('aria-hidden', 'true')
    expect(document.querySelector('.lp-frame')).toHaveAttribute('aria-hidden', 'true')
  })
})

describe('story and metrics content is true', () => {
  it('the budget lines add up to the total', () => {
    expect(BUDGET_LINES.reduce((n, l) => n + l.amount, 0)).toBe(BUDGET_TOTAL)
  })

  it('has exactly five stages and the 17 protocol sections', () => {
    expect(STORY_STAGES).toHaveLength(5)
    expect(PROTOCOL_SECTIONS).toHaveLength(17)
  })

  it('metrics are the verified facts: 17 sections, 4 guideline sets, 8 engines, 3 tools', () => {
    expect(METRICS.map(m => m.value)).toEqual([17, 4, 8, 3])
  })

  it('never claims certification, customers or regulatory approval', () => {
    const text = JSON.stringify([STORY_STAGES, METRICS, PROTOCOL_SECTIONS]).toLowerCase()
    expect(text).not.toMatch(/certified|customers|testimonial|approved by|fda.approved/)
  })
})
