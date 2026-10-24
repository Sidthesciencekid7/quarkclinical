import { describe, expect, it } from 'vitest'
import { sanitizeLaunchTarget } from '../../lib/auth'
import { launchTargetFor, pickActiveSection, SECTION_ORDER, SECTION_TO_NAV } from '../handoff'

describe('launchTargetFor', () => {
  it('builds the chat handoff target with only the fixed kind token', () => {
    expect(launchTargetFor('protocol')).toBe('/app/chat/new?from=landing&kind=protocol')
    expect(launchTargetFor('budget')).toBe('/app/chat/new?from=landing&kind=budget')
  })

  it('is accepted by the launch API sanitiser (same-origin, root-relative)', () => {
    expect(sanitizeLaunchTarget(launchTargetFor('protocol'))).toBe(launchTargetFor('protocol'))
    expect(sanitizeLaunchTarget(launchTargetFor('budget'))).toBe(launchTargetFor('budget'))
  })
})

describe('pickActiveSection (scroll-spy)', () => {
  const line = 270

  it('is null before any section has crossed the line', () => {
    expect(pickActiveSection({ top: 400, products: 1200 }, line)).toBeNull()
  })

  it('picks the last section whose top is at or above the line', () => {
    expect(pickActiveSection({ top: -600, products: 100, chat: 900, how: 1700 }, line)).toBe('products')
    expect(pickActiveSection({ top: -2000, products: -1400, chat: -500, why: 200, how: 900 }, line)).toBe('why')
    expect(pickActiveSection({ top: -3000, products: -2000, how: 270 }, line)).toBe('how')
  })

  it('skips sections that are not on the page', () => {
    expect(pickActiveSection({ top: -10, products: null, how: undefined }, line)).toBe('top')
  })
})

describe('section to nav mapping', () => {
  it('maps every section, and the supporting sections to Products', () => {
    for (const id of SECTION_ORDER) expect(SECTION_TO_NAV).toHaveProperty(id)
    expect(SECTION_TO_NAV.chat).toBe('products')
    expect(SECTION_TO_NAV.why).toBe('products')
    expect(SECTION_TO_NAV.how).toBe('how')
    expect(SECTION_TO_NAV.faq).toBe('faq')
    // Hero and the closing call to action highlight nothing.
    expect(SECTION_TO_NAV.top).toBe('')
    expect(SECTION_TO_NAV.cta).toBe('')
  })
})
