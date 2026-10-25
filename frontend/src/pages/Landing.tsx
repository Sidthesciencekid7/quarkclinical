import { useEffect, useRef, useState } from 'react'
import { buttonClasses, cx } from '../ui'
import { useLaunch } from '../lib/auth'
import { prefersReducedMotion } from '../lib/useReveal'
import ContactSalesModal from '../components/ContactSalesModal'
import { Nav } from '../landing/Nav'
import { Hero } from '../landing/Hero'
import { Story } from '../landing/Story'
import { Metrics } from '../landing/Metrics'
import { Guidance } from '../landing/Guidance'
import { Products } from '../landing/Products'
import { Why } from '../landing/Why'
import { HowItWorks } from '../landing/HowItWorks'
import { Walkthrough } from '../landing/Walkthrough'
import { Pricing } from '../landing/Pricing'
import { Faq } from '../landing/Faq'
import { FinalCta } from '../landing/FinalCta'
import { Footer } from '../landing/Footer'
import { useActiveSection } from '../landing/useActiveSection'
import { useLandingMotion } from '../landing/useLandingMotion'
import '../landing/landing.css'

/**
 * Public landing page: near-black throughout, scroll-driven. The sections live
 * in src/landing/; this file only wires them together and owns the pieces of
 * state that must reach the smooth-scroll engine (dialogs pause it).
 *
 * Motion (Lenis + GSAP) is started by useLandingMotion as a separately loaded
 * chunk and torn down when this page unmounts, so the signed-in app never
 * inherits it. Nothing from the signed-in app (chat, shell, editor) is
 * imported here either.
 */
export default function Landing() {
  const { login } = useLaunch()
  const [contactInterest, setContactInterest] = useState<string | null>(null)
  const [menuOpen, setMenuOpen] = useState(false)
  const active = useActiveSection()
  const rootRef = useRef<HTMLDivElement>(null)
  const composerRef = useRef<HTMLTextAreaElement>(null)
  const motion = useLandingMotion(rootRef)

  // The demo dialog and the mobile menu own the scroll while they are open.
  const { setLocked } = motion
  const locked = menuOpen || contactInterest !== null
  useEffect(() => {
    setLocked(locked)
  }, [locked, setLocked])

  const openDemo = () => setContactInterest('Book a demo')

  // "Start with your study": bring the hero composer into view and focus it.
  const startWithStudy = () => {
    const el = composerRef.current
    if (!el) return
    if (!motion.scrollTo(el, { center: true })) {
      el.scrollIntoView({ block: 'center', behavior: prefersReducedMotion() ? 'auto' : 'smooth' })
    }
    el.focus({ preventScroll: true })
  }

  return (
    <div ref={rootRef} data-surface="dark" className="lp-root overflow-x-clip">
      <a
        href="#main"
        className={cx(
          buttonClasses({ variant: 'primary', size: 'md' }),
          'sr-only focus:not-sr-only focus:fixed focus:left-4 focus:top-4 focus:z-[60] focus:px-4 focus:py-2',
        )}
      >
        Skip to main content
      </a>

      <Nav
        active={active}
        menuOpen={menuOpen}
        onMenuOpenChange={setMenuOpen}
        onLogin={() => login()}
        onDemo={openDemo}
      />

      <main id="main" tabIndex={-1} className="outline-none">
        <Hero onDemo={openDemo} textareaRef={composerRef} />
        <Story />
        <Metrics />
        <Guidance />
        <Products />
        <Why />
        <HowItWorks />
        <Walkthrough />
        <Pricing onContact={setContactInterest} />
        <Faq />
        <FinalCta onDemo={openDemo} onStart={startWithStudy} />
      </main>

      <Footer onContact={openDemo} />

      {contactInterest && (
        <ContactSalesModal initialInterest={contactInterest} onClose={() => setContactInterest(null)} />
      )}
    </div>
  )
}
