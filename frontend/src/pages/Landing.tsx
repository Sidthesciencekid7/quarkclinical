import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { useReveal, spotlightMove } from '../lib/useReveal'
import { useLaunch } from '../lib/auth'
import ContactSalesModal from '../components/ContactSalesModal'
import QuarkLogo from '../components/QuarkLogo'

// Set to a YouTube/Vimeo embed URL once the walkthrough is recorded; the
// video section and the hero "Watch the walkthrough" link stay hidden until then.
const DEMO_VIDEO_EMBED_URL = ''

/* ───────────────────────── helpers ───────────────────────── */

function Reveal({
  children,
  delay = 0,
  className = '',
}: {
  children: React.ReactNode
  delay?: number
  className?: string
}) {
  const { ref, shown } = useReveal<HTMLDivElement>()
  return (
    <div
      ref={ref}
      className={`reveal ${shown ? 'is-visible' : ''} ${className}`}
      style={{ '--reveal-delay': `${delay}ms` } as React.CSSProperties}
    >
      {children}
    </div>
  )
}

/* ───────────────────────── icons (Lucide-style, 1.5 stroke) ───────────────────────── */

const Icon = {
  logo: <QuarkLogo size={22} />,
  arrow: (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" className="transition-transform duration-300 group-hover:translate-x-1">
      <path d="M3 8h10M9 4l4 4-4 4" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  ),
  play: (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none"><path d="M5 3.5v9l7-4.5-7-4.5z" fill="currentColor" /></svg>
  ),
  beaker: (
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none"><path d="M9 3h6M10 3v5L5 18a2 2 0 001.8 3h10.4A2 2 0 0019 18l-5-10V3" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" /><path d="M7.5 14h9" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" /></svg>
  ),
  chart: (
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none"><path d="M4 4v16h16" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" /><path d="M8 14l3-3 3 2 4-5" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" /></svg>
  ),
  shield: (
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none"><path d="M12 3l7 3v5c0 4.5-3 8.3-7 10-4-1.7-7-5.5-7-10V6l7-3z" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round" /><path d="M9 12l2 2 4-4" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" /></svg>
  ),
  doc: (
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none"><path d="M6 3h8l4 4v14a1 1 0 01-1 1H6a1 1 0 01-1-1V4a1 1 0 011-1z" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round" /><path d="M14 3v5h4M8 13h8M8 17h5" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" /></svg>
  ),
  coins: (
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none"><ellipse cx="9" cy="7" rx="5" ry="2.5" stroke="currentColor" strokeWidth="1.6" /><path d="M4 7v4c0 1.4 2.2 2.5 5 2.5s5-1.1 5-2.5V7" stroke="currentColor" strokeWidth="1.6" /><path d="M10 15.5c.6 1.2 2.6 2 5 2 2.8 0 5-1.1 5-2.5v-4c0-1.4-2.2-2.5-5-2.5" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" /></svg>
  ),
  report: (
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none"><rect x="4" y="3" width="16" height="18" rx="2" stroke="currentColor" strokeWidth="1.6" /><path d="M8 16v-3M12 16V9M16 16v-5" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" /></svg>
  ),
  stethoscope: (
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none"><path d="M6 3v6a4 4 0 008 0V3" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" /><path d="M10 13v2a5 5 0 0010 0v-2" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" /><circle cx="20" cy="11" r="2" stroke="currentColor" strokeWidth="1.6" /></svg>
  ),
  lock: (
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none"><rect x="5" y="10" width="14" height="10" rx="2" stroke="currentColor" strokeWidth="1.6" /><path d="M8 10V7a4 4 0 018 0v3" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" /></svg>
  ),
  users: (
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none"><circle cx="9" cy="8" r="3.5" stroke="currentColor" strokeWidth="1.6" /><path d="M2.5 20c.8-3.4 3.4-5.5 6.5-5.5s5.7 2.1 6.5 5.5" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" /><path d="M16 4.6a3.5 3.5 0 010 6.8M18.5 14.8c1.5.8 2.6 2.6 3 5.2" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" /></svg>
  ),
  check: (
    <svg width="14" height="14" viewBox="0 0 14 14" fill="none"><path d="M2.5 7.5l3 3 6-7" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" /></svg>
  ),
  chevron: (
    <svg width="18" height="18" viewBox="0 0 18 18" fill="none"><path d="M4 7l5 5 5-5" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" /></svg>
  ),
}

/* A real product screenshot inside a minimal browser frame. */
function ProductShot({
  src,
  alt,
  label,
  width,
  height,
}: {
  src: string
  alt: string
  label: string
  width: number
  height: number
}) {
  return (
    <figure className="relative rounded-2xl border border-slate-200/80 bg-white shadow-2xl shadow-slate-900/15 overflow-hidden">
      <div className="h-8 flex items-center gap-1.5 px-3.5 border-b border-slate-200 bg-slate-50">
        <span className="w-2.5 h-2.5 rounded-full bg-slate-300" />
        <span className="w-2.5 h-2.5 rounded-full bg-slate-300" />
        <span className="w-2.5 h-2.5 rounded-full bg-slate-300" />
        <span className="ml-3 text-[11px] text-slate-400 font-medium truncate">{label}</span>
      </div>
      <img src={src} alt={alt} width={width} height={height} loading="lazy" decoding="async" className="block w-full h-auto" />
    </figure>
  )
}

/* ───────────────────────── nav ───────────────────────── */

const NAV_LINKS = [
  { label: 'Products', href: '#products' },
  { label: 'How it works', href: '#how' },
  { label: 'Pricing', href: '#pricing' },
  { label: 'FAQ', href: '#faq' },
]

function Nav({ onLogin, onDemo }: { onLogin: () => void; onDemo: () => void }) {
  const [scrolled, setScrolled] = useState(false)
  useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > 16)
    onScroll()
    window.addEventListener('scroll', onScroll, { passive: true })
    return () => window.removeEventListener('scroll', onScroll)
  }, [])

  return (
    <header
      className={[
        'fixed top-0 inset-x-0 z-50 transition-all duration-300',
        scrolled ? 'backdrop-blur-xl bg-[#0B1E3D]/85 border-b border-white/10 shadow-lg shadow-black/20' : 'bg-transparent border-b border-transparent',
      ].join(' ')}
    >
      <div className="max-w-6xl mx-auto px-5 h-16 flex items-center justify-between">
        <a href="#top" className="flex items-center gap-2.5" aria-label="QuarkClinical home">
          <span className="w-9 h-9 rounded-xl bg-cyan-500/15 border border-cyan-400/30 flex items-center justify-center text-cyan-300">
            {Icon.logo}
          </span>
          <span className="text-white font-extrabold tracking-tight text-[15px]">
            Quark<span className="text-cyan-300">Clinical</span>
          </span>
        </a>

        <nav className="hidden md:flex items-center gap-1" aria-label="Main">
          {NAV_LINKS.map(l => (
            <a
              key={l.href}
              href={l.href}
              className="px-3.5 py-2 text-sm font-medium text-slate-300 hover:text-white transition-colors rounded-lg hover:bg-white/5"
            >
              {l.label}
            </a>
          ))}
        </nav>

        <div className="flex items-center gap-2">
          <button
            onClick={onLogin}
            className="inline-flex text-sm font-semibold text-slate-200 hover:text-white px-3.5 py-2 rounded-lg hover:bg-white/5 transition-colors cursor-pointer"
          >
            Log in
          </button>
          <button
            onClick={onDemo}
            className="lift group inline-flex items-center gap-1.5 text-sm font-semibold text-white px-4 py-2 rounded-xl cursor-pointer
                       bg-gradient-to-r from-cyan-500 to-teal-500 shadow-lg shadow-cyan-500/25 hover:shadow-cyan-400/40"
          >
            Book a demo
            {Icon.arrow}
          </button>
        </div>
      </div>
    </header>
  )
}

/* ───────────────────────── hero ───────────────────────── */

// Above-the-fold content is rendered visible immediately (no scroll-reveal),
// so the first paint, link-preview renderers and crawlers all see the message.
function Hero({ onDemo }: { onDemo: () => void }) {
  return (
    <section id="top" className="relative overflow-hidden" style={{ background: 'radial-gradient(120% 80% at 50% 0%, #122a52 0%, #0B1E3D 45%, #081226 100%)' }}>
      <div className="absolute inset-0 hero-grid" />
      <div className="absolute -top-32 -left-24 w-[36rem] h-[36rem] rounded-full blob" style={{ background: 'radial-gradient(circle, rgba(34,211,238,0.20), transparent 60%)' }} />
      <div className="absolute top-10 -right-24 w-[34rem] h-[34rem] rounded-full blob" style={{ background: 'radial-gradient(circle, rgba(13,148,136,0.20), transparent 60%)', animationDelay: '-6s' }} />

      <div className="relative max-w-5xl mx-auto px-5 pt-32 sm:pt-36 pb-16 text-center">
        <p className="inline-flex items-center gap-2 rounded-full border border-cyan-400/25 bg-white/5 px-4 py-1.5 text-xs font-medium text-cyan-100 mb-8">
          <span className="w-1.5 h-1.5 rounded-full bg-cyan-300" />
          For biosimilar and bioequivalence study teams
        </p>

        <h1 className="text-[2.5rem] sm:text-6xl md:text-[4.25rem] font-extrabold leading-[1.05] tracking-tight">
          <span className="text-white">Study protocols and budgets,</span>
          <br />
          <span className="text-gradient">built on computed evidence.</span>
        </h1>

        <p className="mt-6 text-lg sm:text-xl text-slate-300/90 max-w-3xl mx-auto leading-relaxed">
          QuarkClinical drafts biosimilar PK and bioequivalence protocols, prices the study, and analyses the
          results. Sample sizes, sampling schedules and design decisions are calculated by tested engines,
          and key values link to their sources, so your team reviews evidence instead of rewriting a draft.
        </p>

        <div className="mt-9 flex flex-col sm:flex-row items-center justify-center gap-3">
          <button
            onClick={onDemo}
            className="lift group inline-flex items-center gap-2 px-6 py-3.5 rounded-2xl text-base font-bold text-white cursor-pointer
                       bg-gradient-to-r from-cyan-500 to-teal-500 shadow-xl shadow-cyan-500/30 hover:shadow-cyan-400/50"
          >
            Book a demo
            {Icon.arrow}
          </button>
          {DEMO_VIDEO_EMBED_URL ? (
            <a
              href="#walkthrough"
              className="lift inline-flex items-center gap-2 px-6 py-3.5 rounded-2xl text-base font-semibold text-slate-100
                         border border-white/15 bg-white/5 hover:bg-white/10 hover:border-white/25 transition-colors"
            >
              {Icon.play}
              Watch the walkthrough
            </a>
          ) : (
            <a
              href="#how"
              className="lift inline-flex items-center gap-2 px-6 py-3.5 rounded-2xl text-base font-semibold text-slate-100
                         border border-white/15 bg-white/5 hover:bg-white/10 hover:border-white/25 transition-colors"
            >
              See how it works
            </a>
          )}
        </div>

        <p className="mt-5 text-xs text-slate-400">
          Pilot programs available · Drafts for review by qualified experts · FDA, EMA and ICH guidance sets
        </p>
      </div>
      <div className="relative h-24 bg-gradient-to-b from-transparent to-slate-50" />
    </section>
  )
}

/* ───────────────────────── guidance strip ───────────────────────── */

const GUIDANCE = [
  'FDA bioequivalence guidance',
  'FDA biosimilar PK similarity (351(k))',
  'EMA investigation of bioequivalence',
  'EMA similar biological medicinal products',
  'ICH M13A',
  'ICH M11 protocol template',
  'ICH E6 GCP',
  'ICH E9(R1)',
]

const SOURCES = ['PubMed', 'DailyMed labels', 'ClinicalTrials.gov']

function GuidanceStrip() {
  return (
    <section className="bg-slate-50 border-y border-slate-100 py-10" aria-labelledby="guidance-heading">
      <p id="guidance-heading" className="text-center text-xs font-semibold text-slate-500 uppercase tracking-widest mb-6">
        Structured around the guidance your reviewers use
      </p>
      <div className="marquee-mask overflow-hidden">
        <ul className="marquee-track flex gap-4 w-max">
          {[...GUIDANCE, ...GUIDANCE].map((g, i) => (
            <li
              key={i}
              aria-hidden={i >= GUIDANCE.length}
              className="flex-shrink-0 inline-flex items-center gap-2 rounded-xl border border-slate-200 bg-white px-5 py-2.5 text-sm font-semibold text-slate-600 shadow-sm"
            >
              <span className="text-cyan-500">{Icon.shield}</span>
              {g}
            </li>
          ))}
        </ul>
      </div>
      <p className="text-center text-sm text-slate-500 mt-6">
        Grounded in real sources: {SOURCES.join(' · ')}
      </p>
    </section>
  )
}

/* ───────────────────────── products ───────────────────────── */

const PRODUCTS = [
  {
    icon: Icon.doc,
    name: 'Protocol Generator',
    tagline: 'A reviewable protocol draft, section by section.',
    points: [
      'Bioequivalence, food-effect and biosimilar PK study designs',
      'Exact TOST sample size (ABE, RSABE, NTI) locked across every section',
      'Sampling schedule, safety gating, randomization, ICF and CRF plans computed, not written',
      'DOCX and PDF export with versions and page-numbered amendments',
    ],
  },
  {
    icon: Icon.coins,
    name: 'Budgeting Tool',
    tagline: 'A priced study budget from a drug name.',
    points: [
      'Every line priced with its quantity basis and source',
      'Enrollment, sites and countries grounded in the drug’s own ClinicalTrials.gov history',
      'Healthy-volunteer and patient study profiles',
      'Link a protocol to price its real schedule; export to Word',
    ],
  },
  {
    icon: Icon.report,
    name: 'Study Reports',
    tagline: 'From study data to a clinical study report draft.',
    points: [
      'Upload real PK data from the completed study',
      'Non-compartmental analysis and bioequivalence statistics',
      'ICH E3 narrative written around locked results',
      'DOCX and PDF export',
    ],
  },
]

function Products() {
  return (
    <section id="products" className="bg-slate-50 py-24">
      <div className="max-w-6xl mx-auto px-5">
        <Reveal>
          <p className="text-center text-sm font-bold text-cyan-700 uppercase tracking-widest mb-3">One workspace</p>
          <h2 className="text-center text-3xl sm:text-4xl font-extrabold text-slate-900 tracking-tight">Design it, cost it, report it</h2>
          <p className="text-center text-slate-600 mt-4 max-w-2xl mx-auto text-lg">
            Three tools that share one study, so the protocol, the budget and the report stay consistent.
          </p>
        </Reveal>

        <div className="mt-14 grid md:grid-cols-3 gap-5">
          {PRODUCTS.map((p, i) => (
            <Reveal key={p.name} delay={i * 90}>
              <article
                onMouseMove={spotlightMove}
                className="spotlight card-lift h-full rounded-2xl border border-slate-200 bg-white p-7 shadow-sm hover:shadow-xl hover:border-cyan-200"
              >
                <span className="inline-flex w-12 h-12 rounded-xl bg-cyan-50 border border-cyan-100 items-center justify-center text-cyan-700 mb-5">
                  {p.icon}
                </span>
                <h3 className="text-lg font-bold text-slate-900">{p.name}</h3>
                <p className="text-slate-600 text-[15px] mt-1 mb-5">{p.tagline}</p>
                <ul className="space-y-2.5">
                  {p.points.map(pt => (
                    <li key={pt} className="flex items-start gap-2.5 text-sm text-slate-600 leading-relaxed">
                      <span className="mt-1 flex-shrink-0 text-cyan-600">{Icon.check}</span>
                      {pt}
                    </li>
                  ))}
                </ul>
              </article>
            </Reveal>
          ))}
        </div>

        <Reveal className="mt-16">
          <div className="grid lg:grid-cols-5 gap-10 items-center">
            <div className="lg:col-span-2">
              <p className="text-sm font-bold text-cyan-700 uppercase tracking-widest mb-3">Budgeting Tool</p>
              <h3 className="text-2xl sm:text-3xl font-extrabold text-slate-900 tracking-tight">A budget you can defend line by line</h3>
              <p className="text-slate-600 mt-4 text-lg leading-relaxed">
                Type a drug name and get a complete study budget. Each line shows how its quantity was derived and
                where its rate came from, so you can check it against CRO quotes instead of starting from a blank sheet.
              </p>
            </div>
            <div className="lg:col-span-3">
              <ProductShot
                src="/screenshots/budget.webp"
                alt="QuarkClinical Budgeting Tool showing priced line items, each with its quantity, unit cost and the basis for the estimate"
                label="Budgeting Tool"
                width={1032}
                height={640}
              />
            </div>
          </div>
        </Reveal>
      </div>
    </section>
  )
}

/* ───────────────────────── why ───────────────────────── */

const WHY = [
  { icon: Icon.chart, title: 'Exact sample size', body: 'Two one-sided tests on the non-central t distribution, checked against published PowerTOST reference tables. Handles average BE, reference-scaled BE for highly variable drugs, and narrow therapeutic index limits.' },
  { icon: Icon.beaker, title: 'Sourced PK, never invented', body: 'PK parameters come from PubMed abstracts and DailyMed labels with their PMIDs. When the literature has no value, the assumption is flagged, not filled in.' },
  { icon: Icon.stethoscope, title: 'Clinical design rules built in', body: 'Developed with a clinical pharmacologist: healthy volunteers versus patients by safety profile, multiple-dose steady-state PK for target-mediated drugs, and the lowest-variability indication for patient studies.' },
  { icon: Icon.lock, title: 'One set of numbers', body: 'The sample size and design are computed once and locked, so the synopsis, design and statistical sections can never disagree.' },
  { icon: Icon.users, title: 'Review, sign, amend', body: 'Author, reviewer and approver roles, electronic signature, version history and an audit trail on every change.' },
  { icon: Icon.shield, title: 'Built to be checked', body: 'Every design decision carries its rationale and evidence, and every section shows its sources, so a reviewer can verify rather than trust.' },
]

function Why() {
  return (
    <section id="why" className="bg-white py-24">
      <div className="max-w-6xl mx-auto px-5">
        <Reveal>
          <p className="text-center text-sm font-bold text-cyan-700 uppercase tracking-widest mb-3">Why QuarkClinical</p>
          <h2 className="text-center text-3xl sm:text-4xl font-extrabold text-slate-900 tracking-tight">The numbers are computed, not generated</h2>
          <p className="text-center text-slate-600 mt-4 max-w-2xl mx-auto text-lg">
            The language model writes the prose. Everything a regulator would check is calculated by code first and handed to it as fixed facts.
          </p>
        </Reveal>

        <div className="mt-14 grid sm:grid-cols-2 lg:grid-cols-3 gap-5">
          {WHY.map((f, i) => (
            <Reveal key={f.title} delay={(i % 3) * 90}>
              <article className="h-full rounded-2xl border border-slate-200 bg-slate-50/60 p-7">
                <span className="inline-flex w-11 h-11 rounded-xl bg-white border border-slate-200 items-center justify-center text-cyan-700 mb-4">
                  {f.icon}
                </span>
                <h3 className="text-base font-bold text-slate-900 mb-2">{f.title}</h3>
                <p className="text-slate-600 leading-relaxed text-[15px]">{f.body}</p>
              </article>
            </Reveal>
          ))}
        </div>
      </div>
    </section>
  )
}

/* ───────────────────────── how it works ───────────────────────── */

const STEPS = [
  { n: '01', title: 'Choose the drug and study', body: 'Search by INN. Literature, label and prior-trial data are fetched automatically.' },
  { n: '02', title: 'Review the design', body: 'Population, dose, route, design and endpoints are proposed with their reasoning. Accept or change each one.' },
  { n: '03', title: 'Generate and price', body: 'The protocol is drafted around the computed numbers, and the budget is priced from the same study.' },
  { n: '04', title: 'Review, sign and export', body: 'Edit any section, route it through review and approval, and export DOCX or PDF.' },
]

function HowItWorks() {
  return (
    <section id="how" className="bg-slate-50 py-24">
      <div className="max-w-6xl mx-auto px-5">
        <Reveal>
          <p className="text-center text-sm font-bold text-cyan-700 uppercase tracking-widest mb-3">How it works</p>
          <h2 className="text-center text-3xl sm:text-4xl font-extrabold text-slate-900 tracking-tight">From drug name to reviewed draft</h2>
        </Reveal>
        <div className="mt-14 relative grid md:grid-cols-4 gap-6">
          <div className="hidden md:block absolute top-7 left-[12%] right-[12%] h-px bg-gradient-to-r from-cyan-200 via-cyan-300 to-emerald-200" />
          {STEPS.map((s, i) => (
            <Reveal key={s.n} delay={i * 110} className="relative">
              <div className="relative z-10 flex md:flex-col md:items-center gap-4 md:text-center">
                <span className="flex-shrink-0 w-14 h-14 rounded-2xl bg-gradient-to-br from-cyan-500 to-teal-500 text-white font-extrabold flex items-center justify-center shadow-lg shadow-cyan-500/25 text-lg">
                  {s.n}
                </span>
                <div>
                  <h3 className="font-bold text-slate-900 mb-1.5">{s.title}</h3>
                  <p className="text-sm text-slate-600 leading-relaxed">{s.body}</p>
                </div>
              </div>
            </Reveal>
          ))}
        </div>

        <Reveal className="mt-20">
          <div className="grid lg:grid-cols-5 gap-10 items-center">
            <div className="lg:col-span-3 order-2 lg:order-1">
              <ProductShot
                src="/screenshots/review-design.webp"
                alt="The Review design step showing the population, dose and study indication decisions, each with its rationale"
                label="Review design"
                width={654}
                height={556}
              />
            </div>
            <div className="lg:col-span-2 order-1 lg:order-2">
              <h3 className="text-2xl sm:text-3xl font-extrabold text-slate-900 tracking-tight">Every design decision, explained</h3>
              <p className="text-slate-600 mt-4 text-lg leading-relaxed">
                Before anything is written, QuarkClinical shows its reasoning: why patients instead of healthy volunteers,
                why multiple doses to steady state, which indication gives the cleanest PK comparison. You can change
                any of them, or enter your own indication and see how it compares.
              </p>
            </div>
          </div>
        </Reveal>
      </div>
    </section>
  )
}

/* ───────────────────────── walkthrough video (hidden until recorded) ───────────────────────── */

function Walkthrough() {
  if (!DEMO_VIDEO_EMBED_URL) return null
  return (
    <section id="walkthrough" className="bg-white py-24">
      <div className="max-w-5xl mx-auto px-5">
        <Reveal>
          <h2 className="text-center text-3xl sm:text-4xl font-extrabold text-slate-900 tracking-tight mb-10">See a study built end to end</h2>
          <div className="relative rounded-2xl overflow-hidden border border-slate-200 shadow-2xl aspect-video bg-slate-900">
            <iframe
              src={DEMO_VIDEO_EMBED_URL}
              title="QuarkClinical walkthrough"
              className="absolute inset-0 w-full h-full"
              allow="accelerometer; clipboard-write; encrypted-media; gyroscope; picture-in-picture"
              allowFullScreen
              loading="lazy"
            />
          </div>
        </Reveal>
      </div>
    </section>
  )
}

/* ───────────────────────── pricing ───────────────────────── */

const PLANS = [
  {
    name: 'Pilot',
    blurb: 'Prove it on one real study.',
    features: [
      'One study, end to end: protocol and budget',
      'Guided onboarding and a review of the outputs with our team',
      'Fixed scope agreed up front',
      'Move to a plan when you are ready',
    ],
    cta: 'Request a pilot',
    interest: 'Request a pilot on a real study',
    featured: false,
  },
  {
    name: 'Team',
    blurb: 'For sponsors and consultancies running several studies.',
    features: [
      'Protocol Generator and Budgeting Tool',
      'Seats for your team with author, reviewer and approver roles',
      'A monthly usage allowance included',
      'DOCX, PDF and Word exports',
      'Email support',
    ],
    cta: 'Contact us',
    interest: 'Pricing and plans',
    featured: true,
  },
  {
    name: 'Enterprise',
    blurb: 'For larger programs and CROs.',
    features: [
      'All tools, including Study Reports',
      'Higher allowances and annual terms',
      'Priority onboarding and support',
      'Help with security reviews and questionnaires',
    ],
    cta: 'Contact us',
    interest: 'Pricing and plans',
    featured: false,
  },
]

function Pricing({ onContact }: { onContact: (interest: string) => void }) {
  return (
    <section id="pricing" className="bg-white py-24">
      <div className="max-w-6xl mx-auto px-5">
        <Reveal>
          <p className="text-center text-sm font-bold text-cyan-700 uppercase tracking-widest mb-3">Pricing</p>
          <h2 className="text-center text-3xl sm:text-4xl font-extrabold text-slate-900 tracking-tight">Plans for how your team works</h2>
          <p className="text-center text-slate-600 mt-4 text-lg max-w-2xl mx-auto">
            Priced per organization with seats and an included monthly usage allowance. Tell us about your studies and we will send a quote.
          </p>
        </Reveal>

        <div className="mt-12 grid md:grid-cols-3 gap-5 items-stretch">
          {PLANS.map((p, i) => (
            <Reveal key={p.name} delay={i * 90}>
              <div
                className={`h-full rounded-3xl p-8 flex flex-col border ${
                  p.featured
                    ? 'bg-[#0B1E3D] text-white border-cyan-400/40 shadow-2xl shadow-cyan-900/25'
                    : 'bg-white text-slate-900 border-slate-200 shadow-sm'
                }`}
              >
                <h3 className={`text-xl font-bold ${p.featured ? 'text-white' : 'text-slate-900'}`}>{p.name}</h3>
                <p className={`text-sm mt-1 min-h-[2.5rem] ${p.featured ? 'text-slate-300' : 'text-slate-600'}`}>{p.blurb}</p>
                <p className={`mt-4 mb-6 text-2xl font-extrabold ${p.featured ? 'text-white' : 'text-slate-900'}`}>Price on request</p>
                <ul className="space-y-3 mb-8 flex-1">
                  {p.features.map(f => (
                    <li key={f} className={`flex items-start gap-2.5 text-sm ${p.featured ? 'text-slate-200' : 'text-slate-600'}`}>
                      <span className={`mt-0.5 flex-shrink-0 ${p.featured ? 'text-cyan-300' : 'text-cyan-600'}`}>{Icon.check}</span>
                      {f}
                    </li>
                  ))}
                </ul>
                <button
                  onClick={() => onContact(p.interest)}
                  className={`lift w-full py-3 rounded-xl font-bold text-sm cursor-pointer ${
                    p.featured
                      ? 'bg-gradient-to-r from-cyan-500 to-teal-500 text-white shadow-lg shadow-cyan-500/30'
                      : 'border border-slate-300 text-slate-800 hover:border-cyan-400 hover:text-cyan-800'
                  }`}
                >
                  {p.cta}
                </button>
              </div>
            </Reveal>
          ))}
        </div>
      </div>
    </section>
  )
}

/* ───────────────────────── FAQ ───────────────────────── */

const FAQS = [
  { q: 'Is the output ready to submit to a regulator?', a: 'No. QuarkClinical produces drafts for investigational use. Every protocol, budget and report should be reviewed and approved by qualified experts on your team before any regulatory or contractual use. The review, approval and e-signature workflow is built in for that purpose.' },
  { q: 'Does it make up PK values?', a: 'No. PK parameters are extracted from PubMed abstracts and DailyMed labels and tagged with their source. If the literature has no value, the within-subject CV falls back to a clearly flagged assumption, and you can enter your own.' },
  { q: 'How is the sample size calculated?', a: 'With an exact two one-sided tests (TOST) engine using the non-central t distribution, checked against published PowerTOST reference tables. It supports average bioequivalence, reference-scaled bioequivalence for highly variable drugs, and tightened limits for narrow therapeutic index drugs.' },
  { q: 'Which studies are supported?', a: 'Small-molecule bioequivalence and food-effect studies, and biosimilar PK similarity studies in healthy volunteers or patients. For patient studies, QuarkClinical proposes the indication with the lowest expected PK variability and a multiple-dose steady-state design where the drug shows target-mediated disposition, and you can override both.' },
  { q: 'How is a budget priced?', a: 'From a documented rate card, with each line showing its quantity basis and source. Enrollment, site and country counts come from the drug’s own registered trials on ClinicalTrials.gov when they exist, or from your linked protocol or your own inputs. Budgets are estimates to compare against CRO quotes, not quotes themselves.' },
  { q: 'What happens to our study data?', a: 'Each organization has its own workspace and access is controlled by role. Study content is processed by Anthropic’s Claude models to draft text, as listed in our Privacy Policy, and is not used to train models.' },
  { q: 'Can we try it before committing?', a: 'Yes. A pilot runs QuarkClinical on one of your real studies with our team, with the scope agreed up front.' },
]

function FAQ() {
  const [open, setOpen] = useState<number | null>(0)
  return (
    <section id="faq" className="bg-slate-50 py-24">
      <div className="max-w-3xl mx-auto px-5">
        <Reveal>
          <p className="text-center text-sm font-bold text-cyan-700 uppercase tracking-widest mb-3">FAQ</p>
          <h2 className="text-center text-3xl sm:text-4xl font-extrabold text-slate-900 tracking-tight mb-12">Questions, answered</h2>
        </Reveal>
        <div className="space-y-3">
          {FAQS.map((f, i) => {
            const isOpen = open === i
            return (
              <div key={f.q} className={`rounded-2xl border transition-colors ${isOpen ? 'border-cyan-200 bg-cyan-50/40' : 'border-slate-200 bg-white hover:border-slate-300'}`}>
                <button
                  onClick={() => setOpen(isOpen ? null : i)}
                  className="w-full flex items-center justify-between gap-4 px-5 py-4 text-left cursor-pointer"
                  aria-expanded={isOpen}
                >
                  <span className="font-semibold text-slate-900">{f.q}</span>
                  <span className={`flex-shrink-0 text-cyan-700 transition-transform duration-300 ${isOpen ? 'rotate-180' : ''}`}>{Icon.chevron}</span>
                </button>
                <div className={`acc-body ${isOpen ? 'open' : ''}`}>
                  <div>
                    <p className="px-5 pb-5 text-[15px] text-slate-600 leading-relaxed">{f.a}</p>
                  </div>
                </div>
              </div>
            )
          })}
        </div>
      </div>
    </section>
  )
}

/* ───────────────────────── final CTA ───────────────────────── */

function FinalCTA({ onDemo }: { onDemo: () => void }) {
  return (
    <section className="bg-white py-24">
      <div className="max-w-5xl mx-auto px-5">
        <Reveal>
          <div className="relative overflow-hidden rounded-3xl px-8 py-16 text-center" style={{ background: 'radial-gradient(120% 120% at 50% 0%, #143063 0%, #0B1E3D 60%)' }}>
            <div className="absolute inset-0 hero-grid opacity-60" />
            <div className="relative">
              <h2 className="text-3xl sm:text-4xl font-extrabold text-white tracking-tight">See it on a study like yours</h2>
              <p className="mt-4 text-slate-300 text-lg max-w-xl mx-auto">
                Pick a drug you are working on. We will build the protocol and budget with you in a live session.
              </p>
              <button
                onClick={onDemo}
                className="lift group mt-8 inline-flex items-center gap-2 px-7 py-4 rounded-2xl text-base font-bold text-white cursor-pointer bg-gradient-to-r from-cyan-500 to-teal-500 shadow-xl shadow-cyan-500/30"
              >
                Book a demo
                {Icon.arrow}
              </button>
            </div>
          </div>
        </Reveal>
      </div>
    </section>
  )
}

/* ───────────────────────── footer ───────────────────────── */

function Footer({ onContact }: { onContact: () => void }) {
  return (
    <footer className="bg-[#0B1E3D] text-slate-400">
      <div className="max-w-6xl mx-auto px-5 py-14 grid grid-cols-2 md:grid-cols-4 gap-8">
        <div className="col-span-2 md:col-span-1">
          <div className="flex items-center gap-2.5 mb-3">
            <span className="w-9 h-9 rounded-xl bg-cyan-500/15 border border-cyan-400/30 flex items-center justify-center text-cyan-300">{Icon.logo}</span>
            <span className="text-white font-extrabold tracking-tight">Quark<span className="text-cyan-300">Clinical</span></span>
          </div>
          <p className="text-sm leading-relaxed">Protocols, budgets and study reports for clinical pharmacology teams.</p>
        </div>
        <div>
          <p className="text-xs font-bold text-white uppercase tracking-widest mb-4">Product</p>
          <ul className="space-y-2.5">
            {[['Products', '#products'], ['How it works', '#how'], ['Pricing', '#pricing'], ['FAQ', '#faq']].map(([l, href]) => (
              <li key={l}><a href={href} className="text-sm hover:text-cyan-300 transition-colors">{l}</a></li>
            ))}
          </ul>
        </div>
        <div>
          <p className="text-xs font-bold text-white uppercase tracking-widest mb-4">Company</p>
          <ul className="space-y-2.5">
            <li><button onClick={onContact} className="text-sm hover:text-cyan-300 transition-colors cursor-pointer">Book a demo</button></li>
          </ul>
        </div>
        <div>
          <p className="text-xs font-bold text-white uppercase tracking-widest mb-4">Legal</p>
          <ul className="space-y-2.5">
            <li><Link to="/privacy" className="text-sm hover:text-cyan-300 transition-colors">Privacy</Link></li>
            <li><Link to="/terms" className="text-sm hover:text-cyan-300 transition-colors">Terms</Link></li>
          </ul>
        </div>
      </div>
      <div className="border-t border-white/10">
        <div className="max-w-6xl mx-auto px-5 py-6 flex flex-col sm:flex-row items-center justify-between gap-3 text-xs">
          <p>© {new Date().getFullYear()} QuarkClinical. For investigational use; outputs require expert review.</p>
          <p>Audit trail · Role-based review · Electronic signature</p>
        </div>
      </div>
    </footer>
  )
}

/* ───────────────────────── page ───────────────────────── */

export default function Landing() {
  const { login } = useLaunch()
  const [contactInterest, setContactInterest] = useState<string | null>(null)
  const openDemo = () => setContactInterest('Book a demo')

  return (
    <div className="bg-slate-50 overflow-x-hidden">
      <Nav onLogin={login} onDemo={openDemo} />
      <main>
        <Hero onDemo={openDemo} />
        <GuidanceStrip />
        <Products />
        <Why />
        <HowItWorks />
        <Walkthrough />
        <Pricing onContact={setContactInterest} />
        <FAQ />
        <FinalCTA onDemo={openDemo} />
      </main>
      <Footer onContact={openDemo} />
      {contactInterest && (
        <ContactSalesModal initialInterest={contactInterest} onClose={() => setContactInterest(null)} />
      )}
    </div>
  )
}
