/**
 * The pinned "a study being built" story.
 *
 * Desktop (>= 1024px, motion allowed): the track is a tall block with a CSS
 * `position: sticky` stage inside (see landing.css), so there is no JS pin and
 * no pin-spacer. One scrubbed timeline owns the stage:
 *   - progress pill fill (scaleX) over the whole range
 *   - for each stage: its headline + panel blur-cross-fade in (blur 14px,
 *     alpha, small y) and out, and the panel's own build animation (typing,
 *     cards, numbers locking, sections stacking, budget counting) plays in the
 *     middle of the stage's slice of scroll.
 * Phones and tablets (< 1024px): no pin. Each stage fades up as it enters and its build
 * animation plays once.
 *
 * Reduced motion never reaches this file (the engine is not loaded), so the
 * stages stay as plain stacked content.
 *
 * Every number here is an illustration; the DOM always holds the final text
 * and the animation rewrites it, remembering how to put it back.
 */
import gsap from 'gsap'

type Restore = Array<() => void>

const PINNED = '(min-width: 1024px) and (prefers-reduced-motion: no-preference)'
const STACKED = '(max-width: 1023.98px) and (prefers-reduced-motion: no-preference)'

const nf = new Intl.NumberFormat('en-US')

/** Count a number element's text from 0 to its data-to, driven by a tween. */
function countTween(tl: gsap.core.Timeline, el: HTMLElement, at: number, duration: number, restore: Restore, prefix = '') {
  const to = Number(el.dataset.to)
  if (!Number.isFinite(to)) return
  const original = el.textContent ?? ''
  const proxy = { v: 0 }
  el.textContent = prefix + nf.format(0)
  restore.push(() => {
    el.textContent = original
  })
  tl.to(
    proxy,
    {
      v: to,
      duration,
      ease: 'power2.out',
      onUpdate: () => {
        el.textContent = prefix + nf.format(Math.round(proxy.v))
      },
    },
    at,
  )
}

/* ───────────── per-stage build animations (natural seconds) ───────────── */

function buildDescribe(panel: HTMLElement, restore: Restore): gsap.core.Timeline {
  const q = gsap.utils.selector(panel)
  const typed = q('.p1-typed')[0] as HTMLElement | undefined
  const tl = gsap.timeline()
  tl.fromTo(q('.p1-bubble'), { opacity: 0, y: 12 }, { opacity: 1, y: 0, duration: 0.45, ease: 'power2.out' }, 0)
  tl.fromTo(q('.p1-composer'), { opacity: 0, y: 18 }, { opacity: 1, y: 0, duration: 0.5, ease: 'power2.out' }, 0.15)
  if (typed) {
    const full = typed.textContent ?? ''
    const proxy = { n: 0 }
    typed.textContent = ''
    restore.push(() => {
      typed.textContent = full
    })
    tl.to(
      proxy,
      {
        n: full.length,
        duration: 1.5,
        ease: 'none',
        onUpdate: () => {
          typed.textContent = full.slice(0, Math.round(proxy.n))
        },
      },
      0.65,
    )
  }
  tl.fromTo(q('.p1-modes'), { opacity: 0 }, { opacity: 1, duration: 0.4 }, 0.5)
  tl.fromTo(q('.p1-send'), { opacity: 0.3, scale: 0.88 }, { opacity: 1, scale: 1, duration: 0.45, ease: 'back.out(2.4)' }, 2.2)
  return tl
}

function buildConfirm(panel: HTMLElement): gsap.core.Timeline {
  const q = gsap.utils.selector(panel)
  const tl = gsap.timeline()
  tl.fromTo(q('.p2-card'), { opacity: 0, y: 28 }, { opacity: 1, y: 0, duration: 0.6, stagger: 0.6, ease: 'power3.out' }, 0)
  tl.fromTo(q('.p2-chip'), { opacity: 0, scale: 0.85 }, { opacity: 1, scale: 1, duration: 0.35, stagger: 0.12, ease: 'back.out(2)' }, 0.5)
  tl.fromTo(q('.p2-opt'), { opacity: 0, x: -14 }, { opacity: 1, x: 0, duration: 0.4, stagger: 0.14, ease: 'power2.out' }, 1.3)
  tl.fromTo(q('.p2-sel'), { opacity: 0 }, { opacity: 1, duration: 0.4 }, 2.0)
  tl.fromTo(q('.p2-check'), { opacity: 0, scale: 0.4 }, { opacity: 1, scale: 1, duration: 0.4, ease: 'back.out(3)' }, 2.0)
  tl.fromTo(q('.p2-na'), { opacity: 1 }, { opacity: 0.38, duration: 0.5 }, 2.1)
  tl.fromTo(q('.p2-confirmed'), { opacity: 0, y: 6 }, { opacity: 1, y: 0, duration: 0.4, stagger: 0.25, ease: 'power2.out' }, 2.3)
  return tl
}

function buildEngines(panel: HTMLElement, restore: Restore): gsap.core.Timeline {
  const q = gsap.utils.selector(panel)
  const tl = gsap.timeline()
  tl.fromTo(q('.p3-head'), { opacity: 0, y: 10 }, { opacity: 1, y: 0, duration: 0.4, ease: 'power2.out' }, 0)
  const rows = q('.p3-row') as HTMLElement[]
  rows.forEach((row, i) => {
    const at = 0.25 + i * 0.95
    tl.fromTo(row, { opacity: 0, y: 22 }, { opacity: 1, y: 0, duration: 0.5, ease: 'power3.out' }, at)
    const nums = Array.from(row.querySelectorAll<HTMLElement>('.p3-num'))
    nums.forEach(n => countTween(tl, n, at + 0.15, 0.7, restore))
    const tick = row.querySelector('.p3-tick')
    const path = row.querySelector('.p3-tickpath')
    const locked = row.querySelector('.p3-locked')
    if (tick) tl.fromTo(tick, { opacity: 0, scale: 0.4 }, { opacity: 1, scale: 1, duration: 0.4, ease: 'back.out(3)' }, at + 0.75)
    if (path) tl.fromTo(path, { strokeDasharray: 1, strokeDashoffset: 1 }, { strokeDashoffset: 0, duration: 0.35, ease: 'power2.out' }, at + 0.8)
    if (locked) tl.fromTo(locked, { opacity: 0, x: -6 }, { opacity: 1, x: 0, duration: 0.35 }, at + 0.85)
  })
  return tl
}

function buildProtocol(panel: HTMLElement, restore: Restore): gsap.core.Timeline {
  const q = gsap.utils.selector(panel)
  const tl = gsap.timeline()
  tl.fromTo(q('.p4-head'), { opacity: 0, y: 10 }, { opacity: 1, y: 0, duration: 0.4, ease: 'power2.out' }, 0)
  const rows = q('.p4-row') as HTMLElement[]
  const span = 0.09
  tl.fromTo(rows, { opacity: 0, y: 12, x: -8 }, { opacity: 1, y: 0, x: 0, duration: 0.4, stagger: span, ease: 'power2.out' }, 0.3)
  const count = q('.p4-count')[0] as HTMLElement | undefined
  if (count) countTween(tl, count, 0.3, rows.length * span + 0.3, restore)
  tl.fromTo(q('.p4-lock'), { opacity: 0, scale: 0.8 }, { opacity: 1, scale: 1, duration: 0.4, stagger: 0.15, ease: 'back.out(2)' }, 0.3 + rows.length * span)
  return tl
}

function buildBudget(panel: HTMLElement, restore: Restore): gsap.core.Timeline {
  const q = gsap.utils.selector(panel)
  const tl = gsap.timeline()
  tl.fromTo(q('.p5-head'), { opacity: 0, y: 10 }, { opacity: 1, y: 0, duration: 0.4, ease: 'power2.out' }, 0)
  const total = q('.p5-total')[0] as HTMLElement | undefined
  if (total) countTween(tl, total, 0.2, 1.9, restore)
  tl.fromTo(q('.p5-row'), { opacity: 0, y: 12 }, { opacity: 1, y: 0, duration: 0.45, stagger: 0.2, ease: 'power2.out' }, 0.35)
  tl.fromTo(
    q('.p5-bar'),
    { scaleX: 0, transformOrigin: 'left center' },
    { scaleX: 1, duration: 1.1, stagger: 0.2, ease: 'power3.out' },
    0.45,
  )
  tl.fromTo(q('.p5-note'), { opacity: 0 }, { opacity: 1, duration: 0.5 }, 1.8)
  return tl
}

const BUILDERS: Array<(panel: HTMLElement, restore: Restore) => gsap.core.Timeline> = [
  buildDescribe,
  (p) => buildConfirm(p),
  buildEngines,
  buildProtocol,
  buildBudget,
]

/* ───────────── wiring ───────────── */

export function setupStory(root: HTMLElement): gsap.MatchMedia | null {
  const track = root.querySelector<HTMLElement>('.lp-story__track')
  if (!track) return null
  const stages = Array.from(track.querySelectorAll<HTMLElement>('.lp-stage'))
  if (stages.length === 0) return null
  const N = stages.length
  const mm = gsap.matchMedia()

  /* --- desktop: one scrubbed timeline over a sticky stage --- */
  mm.add(PINNED, () => {
    const restore: Restore = []
    const sticky = track.querySelector<HTMLElement>('.lp-story__sticky')
    const fill = track.querySelector<HTMLElement>('.lp-story__fill')
    const stepLabel = track.querySelector<HTMLElement>('.lp-story__step')
    const copies = stages.map(s => s.querySelector<HTMLElement>('.lp-stage__copy')!)
    const panels = stages.map(s => s.querySelector<HTMLElement>('.lp-stage__panel')!)

    const tl = gsap.timeline({
      defaults: { ease: 'none' },
      scrollTrigger: {
        trigger: track,
        start: 'top top',
        end: 'bottom bottom',
        scrub: 0.6,
        invalidateOnRefresh: true,
        onUpdate: self => {
          const idx = Math.min(N - 1, Math.floor(self.progress * N))
          if (sticky && sticky.dataset.stage !== String(idx + 1)) {
            sticky.dataset.stage = String(idx + 1)
            if (stepLabel) stepLabel.textContent = `${idx + 1} / ${N}`
          }
        },
      },
    })

    if (fill) tl.fromTo(fill, { scaleX: 0 }, { scaleX: 1, duration: N }, 0)

    stages.forEach((_, i) => {
      const swap = [copies[i], panels[i]]
      if (i > 0) {
        tl.fromTo(
          swap,
          { opacity: 0, filter: 'blur(14px)', y: 18 },
          { opacity: 1, filter: 'blur(0px)', y: 0, duration: 0.26, ease: 'power2.out' },
          i - 0.02,
        )
      }
      const inner = BUILDERS[i]?.(panels[i], restore)
      if (inner) {
        inner.duration(0.52)
        tl.add(inner, i === 0 ? 0.03 : i + 0.14)
      }
      if (i < N - 1) {
        tl.to(swap, { opacity: 0, filter: 'blur(14px)', y: -18, duration: 0.22, ease: 'power2.in' }, i + 0.8)
      }
    })

    return () => {
      for (const fn of restore) fn()
      if (sticky) delete sticky.dataset.stage
      if (stepLabel) stepLabel.textContent = `1 / ${N}`
      for (const el of [...copies, ...panels]) gsap.set(el, { clearProps: 'opacity,filter,transform,visibility' })
    }
  })

  /* --- phones: no pin, each stage builds as it enters --- */
  mm.add(STACKED, () => {
    const restore: Restore = []
    stages.forEach((stage, i) => {
      const copy = stage.querySelector<HTMLElement>('.lp-stage__copy')
      const panel = stage.querySelector<HTMLElement>('.lp-stage__panel')
      if (copy) {
        gsap.fromTo(
          copy,
          { opacity: 0, y: 34, filter: 'blur(10px)' },
          {
            opacity: 1,
            y: 0,
            filter: 'blur(0px)',
            duration: 0.95,
            ease: 'power3.out',
            clearProps: 'opacity,transform,filter',
            scrollTrigger: { trigger: copy, start: 'top 88%', once: true },
          },
        )
      }
      if (panel) {
        const inner = BUILDERS[i]?.(panel, restore)
        inner?.pause(0)
        gsap.fromTo(
          panel,
          { opacity: 0, y: 34 },
          {
            opacity: 1,
            y: 0,
            duration: 0.9,
            ease: 'power3.out',
            clearProps: 'opacity,transform',
            scrollTrigger: { trigger: panel, start: 'top 85%', once: true, onEnter: () => inner?.play() },
          },
        )
      }
    })
    return () => {
      for (const fn of restore) fn()
    }
  })

  return mm
}
