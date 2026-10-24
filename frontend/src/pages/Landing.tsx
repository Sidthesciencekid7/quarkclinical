import { useRef, useState } from 'react'
import { buttonClasses, cx, Surface } from '../ui'
import { useLaunch } from '../lib/auth'
import { prefersReducedMotion } from '../lib/useReveal'
import ContactSalesModal from '../components/ContactSalesModal'
import { Nav } from '../landing/Nav'
import { Hero } from '../landing/Hero'
import { Guidance } from '../landing/Guidance'
import { Products } from '../landing/Products'
import { TalkItThrough } from '../landing/TalkItThrough'
import { Why } from '../landing/Why'
import { HowItWorks } from '../landing/HowItWorks'
import { Walkthrough } from '../landing/Walkthrough'
import { Pricing } from '../landing/Pricing'
import { Faq } from '../landing/Faq'
import { FinalCta } from '../landing/FinalCta'
import { Footer } from '../landing/Footer'
import { useActiveSection } from '../landing/useActiveSection'
import '../landing/landing.css'

/**
 * Public landing page (redesign F5). Layout: a dark navy nav + hero, light
 * middle sections, a dark final call-to-action card and a dark footer. The
 * sections live in src/landing/; this file only wires them together.
 *
 * Nothing from the signed-in app (chat, shell, editor) is imported here, so
 * the public bundle stays small and never loads app code for a visitor.
 */
export default function Landing() {
  const { login } = useLaunch()
  const [contactInterest, setContactInterest] = useState<string | null>(null)
  const active = useActiveSection()
  const composerRef = useRef<HTMLTextAreaElement>(null)

  const openDemo = () => setContactInterest('Book a demo')

  // "Start with your study": bring the hero composer into view and focus it.
  const startWithStudy = () => {
    const el = composerRef.current
    if (!el) return
    el.scrollIntoView({ block: 'center', behavior: prefersReducedMotion() ? 'auto' : 'smooth' })
    el.focus({ preventScroll: true })
  }

  return (
    <div className="lp-root overflow-x-clip bg-surface-canvas">
      <a
        href="#main"
        data-surface="dark"
        className={cx(
          buttonClasses({ variant: 'primary', size: 'md' }),
          'sr-only focus:not-sr-only focus:fixed focus:left-4 focus:top-4 focus:z-[60] focus:px-4 focus:py-2',
        )}
      >
        Skip to main content
      </a>

      <Nav active={active} onLogin={() => login()} onDemo={openDemo} />

      <main id="main" tabIndex={-1} className="outline-none">
        <Hero onDemo={openDemo} textareaRef={composerRef} />
        <Surface tone="light">
          <Guidance />
          <Products />
          <TalkItThrough />
          <Why />
          <HowItWorks />
          <Walkthrough />
          <Pricing onContact={setContactInterest} />
          <Faq />
          <FinalCta onDemo={openDemo} onStart={startWithStudy} />
        </Surface>
      </main>

      <Footer onContact={openDemo} />

      {contactInterest && (
        <ContactSalesModal initialInterest={contactInterest} onClose={() => setContactInterest(null)} />
      )}
    </div>
  )
}
