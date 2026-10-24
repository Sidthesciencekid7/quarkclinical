import { useEffect, useState } from 'react'
import { SECTION_ORDER, pickActiveSection, type LandingSectionId } from './handoff'

/**
 * Scroll-spy for the landing nav. Reads each section's position on scroll
 * (rAF-throttled) and reports the last section whose top has crossed a line
 * 30% down the viewport. Sections are found by element id, so a section that
 * is not rendered is simply skipped.
 */
export function useActiveSection(): LandingSectionId | null {
  const [active, setActive] = useState<LandingSectionId | null>(null)

  useEffect(() => {
    let frame = 0

    const compute = () => {
      frame = 0
      const line = window.innerHeight * 0.3
      const tops: Partial<Record<LandingSectionId, number | null>> = {}
      for (const id of SECTION_ORDER) {
        const el = document.getElementById(id)
        tops[id] = el ? el.getBoundingClientRect().top : null
      }
      setActive(pickActiveSection(tops, line))
    }
    const schedule = () => {
      if (!frame) frame = window.requestAnimationFrame(compute)
    }

    compute()
    window.addEventListener('scroll', schedule, { passive: true })
    window.addEventListener('resize', schedule)
    return () => {
      window.removeEventListener('scroll', schedule)
      window.removeEventListener('resize', schedule)
      if (frame) window.cancelAnimationFrame(frame)
    }
  }, [])

  return active
}
