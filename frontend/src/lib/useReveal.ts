import { useEffect, useRef, useState } from 'react'

const prefersReducedMotion = () =>
  typeof window !== 'undefined' &&
  window.matchMedia('(prefers-reduced-motion: reduce)').matches

/**
 * Reveal-on-scroll. Adds `is-visible` the first time the element enters the
 * viewport. Pair with the `.reveal` CSS class. Honors reduced-motion by
 * revealing immediately.
 */
export function useReveal<T extends HTMLElement = HTMLDivElement>(
  options: IntersectionObserverInit = { threshold: 0.15, rootMargin: '0px 0px -10% 0px' },
) {
  const ref = useRef<T>(null)
  const [shown, setShown] = useState(false)

  useEffect(() => {
    const el = ref.current
    if (!el || shown) return
    if (prefersReducedMotion()) {
      setShown(true)
      return
    }
    const io = new IntersectionObserver((entries) => {
      for (const e of entries) {
        if (e.isIntersecting) {
          setShown(true)
          io.disconnect()
          break
        }
      }
    }, options)
    io.observe(el)
    return () => io.disconnect()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [shown])

  return { ref, shown }
}

/**
 * Count-up animation that starts when the element scrolls into view.
 * Returns the live display value plus a ref to attach.
 */
export function useCountUp(
  target: number,
  { duration = 1600, decimals = 0 }: { duration?: number; decimals?: number } = {},
) {
  const ref = useRef<HTMLElement>(null)
  const [value, setValue] = useState(0)
  const started = useRef(false)

  useEffect(() => {
    const el = ref.current
    if (!el) return
    if (prefersReducedMotion()) {
      setValue(target)
      return
    }
    const io = new IntersectionObserver((entries) => {
      if (entries[0]?.isIntersecting && !started.current) {
        started.current = true
        const t0 = performance.now()
        const tick = (now: number) => {
          const p = Math.min(1, (now - t0) / duration)
          // ease-out cubic
          const eased = 1 - Math.pow(1 - p, 3)
          setValue(target * eased)
          if (p < 1) requestAnimationFrame(tick)
          else setValue(target)
        }
        requestAnimationFrame(tick)
        io.disconnect()
      }
    }, { threshold: 0.4 })
    io.observe(el)
    return () => io.disconnect()
  }, [target, duration])

  const display = value.toFixed(decimals)
  return { ref, display }
}

/** Updates --mx/--my CSS vars on pointer move for spotlight cards. */
export function spotlightMove(e: React.MouseEvent<HTMLElement>) {
  const el = e.currentTarget
  const r = el.getBoundingClientRect()
  el.style.setProperty('--mx', `${e.clientX - r.left}px`)
  el.style.setProperty('--my', `${e.clientY - r.top}px`)
}
