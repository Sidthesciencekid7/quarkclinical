import { useCallback, useEffect, useLayoutEffect, useRef, type RefObject } from 'react'
import { useReducedMotion } from '../lib/useReveal'
import type { LandingMotion, ScrollToOptions } from './motion/engine'

/** Motion needs a real browser: matchMedia (reduced-motion + the story's
 *  breakpoints). jsdom and very old browsers fail this and get the static page.
 *  `?motion=off` forces the static page too (QA, print, debugging). */
export function motionAvailable(): boolean {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return false
  try {
    return new URLSearchParams(window.location.search).get('motion') !== 'off'
  } catch {
    return true
  }
}

/** How long the page waits for the motion chunk before giving up and showing
 *  everything statically (a stalled network must never leave a blank page). */
const ENGINE_TIMEOUT_MS = 5000

export interface LandingMotionApi {
  /** Smooth-scroll via Lenis. Returns false when Lenis is not running (the
   *  caller then falls back to native scrolling). */
  scrollTo: (target: Element | string | number, opts?: ScrollToOptions) => boolean
  /** Pause / resume smooth scrolling (dialogs, the mobile menu). */
  setLocked: (locked: boolean) => void
}

/**
 * Starts the landing's motion engine (Lenis + GSAP, loaded as a separate chunk)
 * when the page mounts and tears all of it down when it unmounts, so the
 * signed-in app never inherits smooth scrolling or leftover ScrollTriggers.
 *
 * Before first paint it sets `.lp-motion` on the root so elements the engine
 * will animate start hidden instead of flashing; if the engine cannot load, or
 * motion is not allowed (reduced motion, no matchMedia), the class is removed
 * and the page is simply static.
 */
export function useLandingMotion(rootRef: RefObject<HTMLElement>): LandingMotionApi {
  const reduced = useReducedMotion()
  const enabled = motionAvailable() && !reduced
  const engineRef = useRef<LandingMotion | null>(null)
  const lockedRef = useRef(false)

  // Before paint: claim the hidden-until-animated state.
  useLayoutEffect(() => {
    const root = rootRef.current
    if (!root || !enabled) return
    root.classList.add('lp-motion')
    return () => root.classList.remove('lp-motion')
  }, [enabled, rootRef])

  useEffect(() => {
    const root = rootRef.current
    if (!root || !enabled) return
    let cancelled = false
    let engine: LandingMotion | null = null
    let gaveUp = false
    const giveUp = () => {
      gaveUp = true
      root.classList.remove('lp-motion')
    }
    const timer = window.setTimeout(giveUp, ENGINE_TIMEOUT_MS)

    import('./motion/engine')
      .then(mod => {
        // A late arrival after giving up must not half-animate a static page.
        if (cancelled || gaveUp) return
        try {
          engine = mod.initLandingMotion(root)
        } catch (err) {
          console.error('Landing motion failed to start; showing the static page.', err)
          giveUp()
          return
        }
        window.clearTimeout(timer)
        engineRef.current = engine
        if (lockedRef.current) engine.setLocked(true)
      })
      .catch(() => {
        if (!cancelled) giveUp()
      })

    return () => {
      cancelled = true
      window.clearTimeout(timer)
      engine?.destroy()
      engineRef.current = null
    }
  }, [enabled, rootRef])

  const scrollTo = useCallback<LandingMotionApi['scrollTo']>(
    (target, opts) => engineRef.current?.scrollTo(target, opts) ?? false,
    [],
  )
  const setLocked = useCallback((locked: boolean) => {
    lockedRef.current = locked
    engineRef.current?.setLocked(locked)
  }, [])

  return { scrollTo, setLocked }
}
