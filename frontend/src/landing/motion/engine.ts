/**
 * The landing page's motion engine: Lenis smooth scroll + GSAP ScrollTrigger +
 * SplitText, wired to the data attributes the sections carry.
 *
 * This module is imported ONLY through a dynamic `import()` in
 * useLandingMotion, so gsap and lenis ship as a separate chunk that is fetched
 * when the landing route mounts and never touches the signed-in app's bundle.
 *
 * Everything is created inside gsap contexts and a cleanup list, and
 * `destroy()` reverts all of it (ScrollTriggers, SplitText, Lenis, the gsap
 * ticker hook and any text it rewrote), so React 18 StrictMode's double mount
 * and route changes leave nothing behind.
 *
 * Motion map (data attribute -> effect -> trigger):
 *   data-lp="navin"    nav drops in                           on load
 *   data-lp="split"    heading chars: blur 16px + y + alpha  top 82%
 *   data-lp="fade"     fade up from y 36                      top 88%
 *   data-lp="stagger"  its [data-lp-item] children fade up    top 86%
 *   data-lp="count"    number counts 0 -> data-to             top 90%
 *   data-lp="bar"      scaleX 0 -> 1                          top 92%
 *   data-lp="line"     scaleX 0 -> 1, scrubbed with scroll    section range
 *   data-lp="clip"     clip-path inset(100% 0 0 0) -> inset(0) top 82%
 *   data-lp="rise"     scale .93 -> 1, scrubbed with scroll   top 98% -> top 45%
 *   data-lp="parallax" y drift, scrubbed                      section range
 *   data-lp="hero-out" hero content drifts up and dims        hero range
 *   .lp-story__track   pinned story (see story.ts)
 */
import gsap from 'gsap'
import { ScrollTrigger } from 'gsap/ScrollTrigger'
import { SplitText } from 'gsap/SplitText'
import Lenis from 'lenis'
import { setupStory } from './story'

gsap.registerPlugin(ScrollTrigger, SplitText)

export interface ScrollToOptions {
  /** Extra px to subtract from the target's top (nav clearance). */
  offset?: number
  /** Put the element in the middle of the viewport instead of at the top. */
  center?: boolean
  immediate?: boolean
}

export interface LandingMotion {
  /** Smooth-scroll to an element / selector / absolute y. */
  scrollTo(target: Element | string | number, opts?: ScrollToOptions): boolean
  /** Stop (true) or resume (false) smooth scrolling, e.g. while a dialog is open. */
  setLocked(locked: boolean): void
  destroy(): void
}

const num = (v: string | undefined, fallback: number): number => {
  const n = Number(v)
  return v !== undefined && v !== '' && Number.isFinite(n) ? n : fallback
}

const all = <T extends HTMLElement = HTMLElement>(root: ParentNode, selector: string): T[] =>
  Array.from(root.querySelectorAll<T>(selector))

export function initLandingMotion(root: HTMLElement): LandingMotion {
  const cleanups: Array<() => void> = []
  const splits: SplitText[] = []
  let destroyed = false
  let locked = false

  /* ───────────── Lenis, driven by the gsap ticker ───────────── */

  const lenis = new Lenis({ lerp: 0.1, smoothWheel: true, wheelMultiplier: 1 })
  const onLenisScroll = () => ScrollTrigger.update()
  lenis.on('scroll', onLenisScroll)
  const tick = (time: number) => lenis.raf(time * 1000)
  gsap.ticker.add(tick)
  gsap.ticker.lagSmoothing(0)
  cleanups.push(() => {
    gsap.ticker.remove(tick)
    gsap.ticker.lagSmoothing(500, 33)
    lenis.off('scroll', onLenisScroll)
    lenis.destroy()
  })

  // A scroll requested while smooth scrolling is paused (a menu link clicked
  // inside the open mobile sheet) waits for the resume: Lenis.start() resets
  // any animation already running, which would silently cancel it.
  let pending: [Element | string | number, ScrollToOptions] | null = null

  const scrollTo = (target: Element | string | number, opts: ScrollToOptions = {}): boolean => {
    if (destroyed) return false
    if (locked) {
      pending = [target, opts]
      return true
    }
    let y: number
    if (typeof target === 'number') {
      y = target
    } else {
      const el = typeof target === 'string' ? document.querySelector(target) : target
      if (!el) return false
      const rect = el.getBoundingClientRect()
      if (opts.center) {
        y = rect.top + window.scrollY - (window.innerHeight - rect.height) / 2
      } else {
        const margin = parseFloat(getComputedStyle(el).scrollMarginTop) || 0
        y = rect.top + window.scrollY - margin - (opts.offset ?? 0)
      }
    }
    y = Math.max(0, y)
    const distance = Math.abs(y - window.scrollY)
    lenis.scrollTo(y, {
      force: true,
      immediate: opts.immediate,
      duration: Math.min(2.4, Math.max(0.9, distance / 2200 + 0.5)),
    })
    return true
  }

  // In-page anchors (nav, "See how it works", footer links, the skip link).
  const onClick = (e: MouseEvent) => {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return
    const a = (e.target as Element | null)?.closest('a[href^="#"]') as HTMLAnchorElement | null
    if (!a) return
    const id = decodeURIComponent(a.getAttribute('href')!.slice(1))
    if (!id) return
    const el = document.getElementById(id)
    if (!el) return
    e.preventDefault()
    scrollTo(el)
    // Keep the skip link honest: move focus to the target when it can take it.
    if (el.tabIndex >= -1 && el.hasAttribute('tabindex')) el.focus({ preventScroll: true })
  }
  root.addEventListener('click', onClick)
  cleanups.push(() => root.removeEventListener('click', onClick))

  /* ───────────── element effects ───────────── */

  const ctx = gsap.context(() => {
    // Nav drops in.
    all(root, '[data-lp="navin"]').forEach(el => {
      el.classList.add('lp-in')
      gsap.fromTo(
        el,
        { opacity: 0, y: -18 },
        { opacity: 1, y: 0, duration: 0.9, ease: 'power3.out', delay: 0.15, clearProps: 'opacity,transform' },
      )
    })

    // Headings: chars blur in (SplitText keeps an aria-label for screen readers).
    all(root, '[data-lp="split"]').forEach(el => {
      const split = SplitText.create(el, { type: 'words,chars' })
      splits.push(split)
      el.classList.add('lp-in')
      const chars = split.chars
      gsap.fromTo(
        chars,
        { autoAlpha: 0, y: 14, filter: 'blur(16px)' },
        {
          autoAlpha: 1,
          y: 0,
          filter: 'blur(0px)',
          duration: 0.95,
          ease: 'power3.out',
          delay: num(el.dataset.lpDelay, 0),
          stagger: { amount: Math.min(0.75, chars.length * 0.022) },
          clearProps: 'opacity,visibility,transform,filter',
          scrollTrigger: { trigger: el, start: 'top 82%', once: true },
        },
      )
    })

    // Single elements fade up.
    all(root, '[data-lp="fade"]').forEach(el => {
      el.classList.add('lp-in')
      gsap.fromTo(
        el,
        { opacity: 0, y: 36 },
        {
          opacity: 1,
          y: 0,
          duration: 1,
          ease: 'power3.out',
          delay: num(el.dataset.lpDelay, 0),
          clearProps: 'opacity,transform',
          // Hero items near the bottom of the first screen must still appear
          // on load, so they trigger as soon as any part is on screen.
          scrollTrigger: { trigger: el, start: el.closest('#top') ? 'top 100%' : 'top 88%', once: true },
        },
      )
    })

    // Groups: children fade up in sequence.
    all(root, '[data-lp="stagger"]').forEach(group => {
      const items = all(group, '[data-lp-item]')
      if (!items.length) return
      items.forEach(i => i.classList.add('lp-in'))
      gsap.fromTo(
        items,
        { opacity: 0, y: 40 },
        {
          opacity: 1,
          y: 0,
          duration: 0.95,
          ease: 'power3.out',
          stagger: num(group.dataset.lpStagger, 0.1),
          clearProps: 'opacity,transform',
          scrollTrigger: { trigger: group, start: 'top 86%', once: true },
        },
      )
    })

    // Count-ups (the real number is in the DOM; screen readers get a static copy).
    all(root, '[data-lp="count"]').forEach(el => {
      const to = num(el.dataset.to, NaN)
      if (!Number.isFinite(to)) return
      const nf = new Intl.NumberFormat('en-US')
      const original = el.textContent ?? ''
      const proxy = { v: 0 }
      el.textContent = nf.format(0)
      cleanups.push(() => {
        el.textContent = original
      })
      gsap.to(proxy, {
        v: to,
        duration: 1.9,
        ease: 'power2.out',
        delay: num(el.dataset.lpDelay, 0),
        onUpdate: () => {
          el.textContent = nf.format(Math.round(proxy.v))
        },
        onComplete: () => {
          el.textContent = nf.format(to)
        },
        scrollTrigger: { trigger: el, start: 'top 90%', once: true },
      })
    })

    // Bars fill.
    all(root, '[data-lp="bar"]').forEach(el => {
      el.classList.add('lp-in')
      gsap.fromTo(
        el,
        { scaleX: 0, transformOrigin: 'left center' },
        {
          scaleX: 1,
          duration: 1.5,
          ease: 'power3.inOut',
          delay: num(el.dataset.lpDelay, 0),
          clearProps: 'transform',
          scrollTrigger: { trigger: el, start: 'top 94%', once: true },
        },
      )
    })

    // A line drawn by scrolling (How it works).
    all(root, '[data-lp="line"]').forEach(el => {
      el.classList.add('lp-in')
      const section = el.closest('section') ?? el
      gsap.fromTo(
        el,
        { scaleX: 0, transformOrigin: 'left center' },
        {
          scaleX: 1,
          ease: 'none',
          scrollTrigger: { trigger: section, start: 'top 55%', end: 'bottom 70%', scrub: 0.4 },
        },
      )
    })

    // Media reveals: clip-path wipe + a slow settle on the picture.
    all(root, '[data-lp="clip"]').forEach(el => {
      el.classList.add('lp-in')
      const media = el.querySelector('img')
      gsap.fromTo(
        el,
        { clipPath: 'inset(100% 0% 0% 0%)' },
        {
          clipPath: 'inset(0% 0% 0% 0%)',
          duration: 1.3,
          ease: 'power4.inOut',
          clearProps: 'clipPath',
          scrollTrigger: { trigger: el, start: 'top 82%', once: true },
        },
      )
      if (media) {
        gsap.fromTo(
          media,
          { scale: 1.16 },
          {
            scale: 1,
            duration: 1.8,
            ease: 'power3.out',
            clearProps: 'transform',
            scrollTrigger: { trigger: el, start: 'top 82%', once: true },
          },
        )
      }
    })

    // Frames grow into place as they approach the middle of the screen.
    all(root, '[data-lp="rise"]').forEach(el => {
      gsap.fromTo(
        el,
        { scale: 0.93, y: 40 },
        {
          scale: 1,
          y: 0,
          ease: 'none',
          scrollTrigger: { trigger: el, start: 'top 98%', end: 'top 45%', scrub: 0.5 },
        },
      )
    })

    // Slow drift on decorative glows.
    all(root, '[data-lp="parallax"]').forEach(el => {
      const speed = num(el.dataset.speed, 0.2)
      const section = el.closest('section') ?? el
      gsap.fromTo(
        el,
        { y: () => -window.innerHeight * speed },
        {
          y: () => window.innerHeight * speed,
          ease: 'none',
          scrollTrigger: { trigger: section, start: 'top bottom', end: 'bottom top', scrub: true, invalidateOnRefresh: true },
        },
      )
    })

    // The hero's content lifts and dims as it leaves.
    all(root, '[data-lp="hero-out"]').forEach(el => {
      const section = el.closest('section') ?? el
      gsap.to(el, {
        y: -70,
        scale: 0.965,
        opacity: 0.12,
        transformOrigin: '50% 0%',
        ease: 'power2.in',
        scrollTrigger: { trigger: section, start: 'top top', end: 'bottom 25%', scrub: true },
      })
    })
  }, root)
  cleanups.push(() => ctx.revert())

  /* ───────────── the pinned story ───────────── */

  const story = setupStory(root)
  if (story) cleanups.push(() => story.revert())

  /* ───────────── keep trigger positions honest ───────────── */

  let refreshTimer = 0
  const refreshSoon = () => {
    window.clearTimeout(refreshTimer)
    refreshTimer = window.setTimeout(() => {
      if (!destroyed) ScrollTrigger.refresh()
    }, 200)
  }
  if (typeof ResizeObserver !== 'undefined') {
    const ro = new ResizeObserver(refreshSoon)
    ro.observe(root)
    cleanups.push(() => ro.disconnect())
  }
  document.fonts?.ready.then(refreshSoon).catch(() => {})
  window.addEventListener('load', refreshSoon)
  cleanups.push(() => {
    window.clearTimeout(refreshTimer)
    window.removeEventListener('load', refreshSoon)
  })

  // A deep link (/#pricing) lands on its section once triggers are measured.
  if (window.location.hash.length > 1) {
    const el = document.getElementById(decodeURIComponent(window.location.hash.slice(1)))
    if (el) requestAnimationFrame(() => scrollTo(el, { immediate: true }))
  }

  return {
    scrollTo,
    setLocked(next: boolean) {
      if (destroyed || next === locked) return
      locked = next
      if (next) {
        lenis.stop()
        return
      }
      lenis.start()
      if (pending) {
        const [target, opts] = pending
        pending = null
        requestAnimationFrame(() => scrollTo(target, opts))
      }
    },
    destroy() {
      if (destroyed) return
      destroyed = true
      for (const fn of cleanups.reverse()) {
        try {
          fn()
        } catch {
          /* keep tearing down */
        }
      }
      for (const s of splits) s.revert()
      // Only the landing creates ScrollTriggers in this app; make sure none survive.
      ScrollTrigger.getAll().forEach(t => t.kill())
    },
  }
}
