import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { useReveal, useCountUp, spotlightMove } from '../lib/useReveal'
import { useLaunch } from '../lib/auth'
import ContactSalesModal from '../components/ContactSalesModal'

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
  logo: (
    <svg width="22" height="22" viewBox="0 0 20 20" fill="none">
      <path d="M4 3h9l4 4v11a1 1 0 01-1 1H4a1 1 0 01-1-1V4a1 1 0 011-1z" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
      <path d="M13 3v5h4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M7 11h6M7 14h4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  ),
  arrow: (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" className="transition-transform duration-300 group-hover:translate-x-1">
      <path d="M3 8h10M9 4l4 4-4 4" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  ),
  spark: (
    <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
      <path d="M7 1l1.4 3.9L12 6.3 8.4 7.7 7 11.6 5.6 7.7 2 6.3l3.6-1.4L7 1z" fill="currentColor" />
    </svg>
  ),
  beaker: (
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none"><path d="M9 3h6M10 3v5L5 18a2 2 0 001.8 3h10.4A2 2 0 0019 18l-5-10V3" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" /><path d="M7.5 14h9" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" /></svg>
  ),
  chart: (
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none"><path d="M4 4v16h16" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" /><path d="M8 14l3-3 3 2 4-5" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" /></svg>
  ),
  book: (
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none"><path d="M4 5a2 2 0 012-2h6v18H6a2 2 0 01-2-2V5z" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round" /><path d="M12 3h6a2 2 0 012 2v14a2 2 0 01-2 2h-6" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round" /></svg>
  ),
  shield: (
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none"><path d="M12 3l7 3v5c0 4.5-3 8.3-7 10-4-1.7-7-5.5-7-10V6l7-3z" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round" /><path d="M9 12l2 2 4-4" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" /></svg>
  ),
  doc: (
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none"><path d="M6 3h8l4 4v14a1 1 0 01-1 1H6a1 1 0 01-1-1V4a1 1 0 011-1z" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round" /><path d="M14 3v5h4M8 13h8M8 17h5" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" /></svg>
  ),
  bolt: (
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none"><path d="M13 2L4 14h7l-1 8 9-12h-7l1-8z" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round" /></svg>
  ),
  check: (
    <svg width="14" height="14" viewBox="0 0 14 14" fill="none"><path d="M2.5 7.5l3 3 6-7" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" /></svg>
  ),
  chevron: (
    <svg width="18" height="18" viewBox="0 0 18 18" fill="none"><path d="M4 7l5 5 5-5" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" /></svg>
  ),
}

/* ───────────────────────── nav ───────────────────────── */

const NAV_LINKS = [
  { label: 'Features', href: '#features' },
  { label: 'How it works', href: '#how' },
  { label: 'Pricing', href: '#pricing' },
  { label: 'FAQ', href: '#faq' },
]

function Nav({ onLogin, onSignup }: { onLogin: () => void; onSignup: () => void }) {
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
        scrolled ? 'backdrop-blur-xl bg-[#0B1E3D]/80 border-b border-white/10 shadow-lg shadow-black/20' : 'bg-transparent border-b border-transparent',
      ].join(' ')}
    >
      <div className="max-w-6xl mx-auto px-5 h-16 flex items-center justify-between">
        <a href="#top" className="flex items-center gap-2.5 group">
          <span className="w-9 h-9 rounded-xl bg-cyan-500/15 border border-cyan-400/30 flex items-center justify-center text-cyan-300 lift">
            {Icon.logo}
          </span>
          <span className="text-white font-extrabold tracking-tight text-[15px]">
            Quark <span className="text-cyan-300">AI</span>
          </span>
        </a>

        <nav className="hidden md:flex items-center gap-1">
          {NAV_LINKS.map(l => (
            <a
              key={l.href}
              href={l.href}
              className="relative px-3.5 py-2 text-sm font-medium text-slate-300 hover:text-white transition-colors rounded-lg hover:bg-white/5"
            >
              {l.label}
            </a>
          ))}
        </nav>

        <div className="flex items-center gap-2">
          <button
            onClick={onLogin}
            className="hidden sm:inline-flex text-sm font-semibold text-slate-200 hover:text-white px-3.5 py-2 rounded-lg hover:bg-white/5 transition-colors cursor-pointer"
          >
            Log in
          </button>
          <button
            onClick={onSignup}
            className="lift group inline-flex items-center gap-1.5 text-sm font-semibold text-white px-4 py-2 rounded-xl cursor-pointer
                       bg-gradient-to-r from-cyan-500 to-teal-500 shadow-lg shadow-cyan-500/25 hover:shadow-cyan-400/40"
          >
            Sign up
            {Icon.arrow}
          </button>
        </div>
      </div>
    </header>
  )
}

/* ───────────────────────── hero ───────────────────────── */

function Hero({ onLaunch }: { onLaunch: () => void }) {
  return (
    <section id="top" className="relative overflow-hidden" style={{ background: 'radial-gradient(120% 80% at 50% 0%, #122a52 0%, #0B1E3D 45%, #081226 100%)' }}>
      {/* dot grid */}
      <div className="absolute inset-0 hero-grid" />
      {/* aurora blobs */}
      <div className="absolute -top-32 -left-24 w-[36rem] h-[36rem] rounded-full blob" style={{ background: 'radial-gradient(circle, rgba(34,211,238,0.22), transparent 60%)' }} />
      <div className="absolute top-10 -right-24 w-[34rem] h-[34rem] rounded-full blob" style={{ background: 'radial-gradient(circle, rgba(13,148,136,0.22), transparent 60%)', animationDelay: '-6s' }} />
      {/* floating particles */}
      {[
        { l: '14%', t: '28%', d: '0s' }, { l: '82%', t: '22%', d: '1.2s' },
        { l: '24%', t: '64%', d: '2.1s' }, { l: '70%', t: '60%', d: '0.6s' },
        { l: '50%', t: '16%', d: '1.7s' },
      ].map((p, i) => (
        <span key={i} className="absolute w-1.5 h-1.5 rounded-full bg-cyan-300/70 floaty" style={{ left: p.l, top: p.t, animationDelay: p.d }} />
      ))}

      <div className="relative max-w-4xl mx-auto px-5 pt-36 pb-0 text-center">
        {/* announcement pill */}
        <Reveal>
          <button
            onClick={onLaunch}
            className="shimmer relative overflow-hidden inline-flex items-center gap-2 rounded-full border border-cyan-400/25 bg-white/5 backdrop-blur px-4 py-1.5 text-xs font-medium text-cyan-100 hover:border-cyan-300/50 transition-colors cursor-pointer mb-8"
          >
            <span className="text-cyan-300">{Icon.spark}</span>
            Introducing Quark AI — exact-statistics protocol drafting
            <span className="text-cyan-300">→</span>
          </button>
        </Reveal>

        {/* headline */}
        <Reveal delay={80}>
          <h1 className="text-[2.7rem] sm:text-6xl md:text-7xl font-extrabold leading-[1.04] tracking-tight">
            <span className="text-gradient">FDA-ready protocols,</span>
            <br />
            <span className="text-white">drafted in minutes.</span>
          </h1>
        </Reveal>

        {/* subtitle */}
        <Reveal delay={160}>
          <p className="mt-6 text-lg sm:text-xl text-slate-300/90 max-w-2xl mx-auto leading-relaxed">
            Generate bioequivalence and biosimilar PK study protocols with literature-backed
            PK parameters, exact TOST sample-size calculation, and guideline-mapped sections —
            all reviewable, editable, and export-ready.
          </p>
        </Reveal>

        {/* CTAs */}
        <Reveal delay={240}>
          <div className="mt-9 flex flex-col sm:flex-row items-center justify-center gap-3">
            <button
              onClick={onLaunch}
              className="lift group inline-flex items-center gap-2 px-6 py-3.5 rounded-2xl text-base font-bold text-white cursor-pointer
                         bg-gradient-to-r from-cyan-500 to-teal-500 shadow-xl shadow-cyan-500/30 hover:shadow-cyan-400/50"
            >
              Get started for free
              {Icon.arrow}
            </button>
            <a
              href="#how"
              className="lift inline-flex items-center gap-2 px-6 py-3.5 rounded-2xl text-base font-semibold text-slate-100
                         border border-white/15 bg-white/5 backdrop-blur hover:bg-white/10 hover:border-white/25 transition-colors"
            >
              See how it works
            </a>
          </div>
        </Reveal>

        <Reveal delay={320}>
          <p className="mt-5 text-xs text-slate-400 flex items-center justify-center gap-2">
            <span className="text-emerald-400">{Icon.check}</span>
            No credit card required · Investigational use · Reviewed by clinical pharmacologists
          </p>
        </Reveal>

        {/* product preview peeking from bottom */}
        <Reveal delay={120} className="mt-16">
          <div className="tilt-3d relative">
            <div className="absolute -inset-6 bg-cyan-500/20 blur-3xl rounded-[2rem]" />
            <ProductMockup />
          </div>
        </Reveal>
      </div>
      {/* fade into next section */}
      <div className="relative h-24 bg-gradient-to-b from-transparent to-slate-50" />
    </section>
  )
}

/* A miniature of the real protocol editor, in-brand. */
function ProductMockup() {
  const sections = ['Title Page', 'Synopsis', 'Objectives', 'Study Design', 'Subject Selection', 'PK Sampling', 'Statistical Analysis']
  return (
    <div className="relative rounded-2xl border border-white/10 bg-[#0d2244] shadow-2xl shadow-black/50 overflow-hidden text-left">
      {/* window bar */}
      <div className="h-9 flex items-center gap-1.5 px-4 border-b border-white/8 bg-[#0b1d3a]">
        <span className="w-2.5 h-2.5 rounded-full bg-red-400/70" />
        <span className="w-2.5 h-2.5 rounded-full bg-amber-400/70" />
        <span className="w-2.5 h-2.5 rounded-full bg-emerald-400/70" />
        <span className="ml-3 text-[11px] text-slate-400 font-medium">Quark AI — Dulaglutide · Biosimilar PK</span>
      </div>
      <div className="flex h-[320px]">
        {/* sidebar */}
        <div className="w-44 flex-shrink-0 border-r border-white/8 bg-[#0b1d3a] p-3 hidden sm:block">
          <p className="text-[9px] font-bold text-slate-500 uppercase tracking-widest mb-2">Sections</p>
          <div className="h-1 bg-white/8 rounded-full mb-3 overflow-hidden">
            <div className="h-full w-3/4 bg-gradient-to-r from-cyan-400 to-emerald-400 rounded-full" />
          </div>
          <div className="space-y-1">
            {sections.map((s, i) => (
              <div key={s} className={`flex items-center gap-2 text-[11px] px-2 py-1.5 rounded-lg ${i === 6 ? 'bg-cyan-500/15 text-cyan-200' : 'text-slate-400'}`}>
                <span className={`w-1.5 h-1.5 rounded-full ${i < 6 ? 'bg-emerald-400/80' : 'bg-cyan-400'}`} />
                <span className="truncate">{s}</span>
              </div>
            ))}
          </div>
        </div>
        {/* main */}
        <div className="flex-1 p-5 overflow-hidden bg-slate-50">
          <div className="flex items-center gap-2 mb-1">
            <span className="text-[10px] font-bold text-white bg-cyan-600 rounded-md w-5 h-5 flex items-center justify-center">7</span>
            <h3 className="text-[15px] font-bold text-slate-900">Statistical Analysis Plan</h3>
          </div>
          <p className="text-[11px] text-slate-500 mb-3">Sample size determined by exact two one-sided tests (TOST).</p>
          <div className="rounded-xl border border-slate-200 bg-white overflow-hidden text-[11px]">
            {[
              ['Within-subject CV', '24.5%'],
              ['Geometric mean ratio', '0.95'],
              ['Power / α', '90% / 0.05'],
              ['Acceptance limits', '80.00–125.00%'],
            ].map(([k, v], i) => (
              <div key={k} className={`flex justify-between px-3 py-2 ${i % 2 ? 'bg-slate-50/60' : ''}`}>
                <span className="text-slate-500">{k}</span>
                <span className="font-semibold text-slate-800 tabular-nums">{v}</span>
              </div>
            ))}
            <div className="flex justify-between px-3 py-2.5 bg-cyan-50 border-t border-cyan-100">
              <span className="font-semibold text-cyan-800">Subjects required (incl. dropout)</span>
              <span className="font-extrabold text-cyan-700 tabular-nums">40</span>
            </div>
          </div>
          <div className="mt-3 space-y-1.5">
            <div className="h-2 bg-slate-200 rounded-full w-full" />
            <div className="h-2 bg-slate-200 rounded-full w-11/12" />
            <div className="h-2 bg-slate-200 rounded-full w-4/5" />
          </div>
        </div>
      </div>
    </div>
  )
}

/* ───────────────────────── trust marquee ───────────────────────── */

const COMPLIANCE = ['FDA BE 2014', '21 CFR § 320', 'ICH E6(R2) GCP', 'ICH E9(R1)', 'ICH M11', 'EMA Bioequivalence', 'DailyMed SPL', '21 CFR Part 11']

function TrustMarquee() {
  return (
    <section className="bg-slate-50 border-y border-slate-100 py-10">
      <p className="text-center text-xs font-semibold text-slate-400 uppercase tracking-widest mb-6">
        Mapped to the guidance regulators expect
      </p>
      <div className="marquee-mask overflow-hidden">
        <div className="marquee-track flex gap-4 w-max">
          {[...COMPLIANCE, ...COMPLIANCE].map((c, i) => (
            <span key={i} className="flex-shrink-0 inline-flex items-center gap-2 rounded-xl border border-slate-200 bg-white px-5 py-2.5 text-sm font-semibold text-slate-600 shadow-sm">
              <span className="text-cyan-500">{Icon.shield}</span>
              {c}
            </span>
          ))}
        </div>
      </div>
    </section>
  )
}

/* ───────────────────────── stats ───────────────────────── */

function Stat({ target, decimals = 0, prefix = '', suffix = '', label }: { target: number; decimals?: number; prefix?: string; suffix?: string; label: string }) {
  const { ref, display } = useCountUp(target, { decimals })
  return (
    <div className="text-center">
      <p ref={ref as React.RefObject<HTMLParagraphElement>} className="text-4xl sm:text-5xl font-extrabold text-slate-900 tabular-nums">
        {prefix}{display}{suffix}
      </p>
      <p className="mt-2 text-sm font-medium text-slate-500">{label}</p>
    </div>
  )
}

function Stats() {
  return (
    <section className="bg-white py-16">
      <div className="max-w-5xl mx-auto px-5 grid grid-cols-2 md:grid-cols-4 gap-10">
        <Reveal delay={0}><Stat target={14} label="Guideline-mapped sections" /></Reveal>
        <Reveal delay={80}><Stat target={2} prefix="<" suffix=" min" label="From drug to full draft" /></Reveal>
        <Reveal delay={160}><Stat target={90} suffix="%" label="Default statistical power" /></Reveal>
        <Reveal delay={240}><Stat target={100} suffix="%" label="Exact TOST, no approximation" /></Reveal>
      </div>
    </section>
  )
}

/* ───────────────────────── features (bento) ───────────────────────── */

const FEATURES = [
  { icon: Icon.beaker, title: 'Literature-backed PK', body: 'Pulls PubMed abstracts and DailyMed SPL labels, then extracts PK parameters with source PMIDs — never fabricated.', span: 'md:col-span-2' },
  { icon: Icon.chart, title: 'Exact sample size', body: 'Non-central t TOST engine validated against PowerTOST. Handles ABE, RSABE for highly-variable drugs, and NTI limits.', span: '' },
  { icon: Icon.shield, title: 'One source of truth for N', body: 'Sample size computed once and locked across synopsis, design, and SAP — sections can never disagree.', span: '' },
  { icon: Icon.doc, title: 'Export-ready DOCX & PDF', body: 'Cover, abbreviations, schedule of activities, sample-size tables, and regulatory references — formatted on export.', span: 'md:col-span-2' },
  { icon: Icon.book, title: 'BE & biosimilar pathways', body: 'Small-molecule bioequivalence or biologic PK with mandatory immunogenicity — the right sections appear automatically.', span: '' },
  { icon: Icon.bolt, title: 'Editable & regenerable', body: 'Rich editor with per-section regeneration, inline notes, and source tracing. You stay in control of every word.', span: 'md:col-span-2' },
]

function Features() {
  return (
    <section id="features" className="bg-slate-50 py-24">
      <div className="max-w-6xl mx-auto px-5">
        <Reveal>
          <p className="text-center text-sm font-bold text-cyan-600 uppercase tracking-widest mb-3">Why Quark AI</p>
          <h2 className="text-center text-3xl sm:text-4xl font-extrabold text-slate-900 tracking-tight">Rigor where it matters most</h2>
          <p className="text-center text-slate-500 mt-4 max-w-2xl mx-auto text-lg">
            Not a chatbot wrapper. Real biostatistics, real literature, real regulatory structure — built for clinical pharmacology.
          </p>
        </Reveal>

        <div className="mt-14 grid md:grid-cols-3 gap-5">
          {FEATURES.map((f, i) => (
            <Reveal key={f.title} delay={(i % 3) * 90} className={f.span}>
              <article
                onMouseMove={spotlightMove}
                className="spotlight card-lift h-full rounded-2xl border border-slate-200 bg-white p-7 shadow-sm hover:shadow-xl hover:border-cyan-200"
              >
                <span className="inline-flex w-12 h-12 rounded-xl bg-cyan-50 border border-cyan-100 items-center justify-center text-cyan-600 mb-5">
                  {f.icon}
                </span>
                <h3 className="text-lg font-bold text-slate-900 mb-2">{f.title}</h3>
                <p className="text-slate-500 leading-relaxed text-[15px]">{f.body}</p>
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
  { n: '01', title: 'Select your drug', body: 'Search by INN. We fetch the relevant PubMed literature and label data automatically.' },
  { n: '02', title: 'Configure the study', body: 'Choose BE or biosimilar PK, set the population, and optionally supply CV%, RLD, and NTI status.' },
  { n: '03', title: 'Generate & compute', body: 'Sample size is computed exactly, then every guideline-mapped section is drafted against it.' },
  { n: '04', title: 'Review & export', body: 'Edit any section, regenerate, trace sources, and export a formatted DOCX or PDF.' },
]

function HowItWorks() {
  return (
    <section id="how" className="bg-white py-24">
      <div className="max-w-5xl mx-auto px-5">
        <Reveal>
          <p className="text-center text-sm font-bold text-cyan-600 uppercase tracking-widest mb-3">How it works</p>
          <h2 className="text-center text-3xl sm:text-4xl font-extrabold text-slate-900 tracking-tight">Four steps to a full draft</h2>
        </Reveal>
        <div className="mt-14 relative grid md:grid-cols-4 gap-6">
          {/* connecting line */}
          <div className="hidden md:block absolute top-7 left-[12%] right-[12%] h-px bg-gradient-to-r from-cyan-200 via-cyan-300 to-emerald-200" />
          {STEPS.map((s, i) => (
            <Reveal key={s.n} delay={i * 110} className="relative">
              <div className="relative z-10 flex md:flex-col md:items-center gap-4 md:text-center">
                <span className="flex-shrink-0 w-14 h-14 rounded-2xl bg-gradient-to-br from-cyan-500 to-teal-500 text-white font-extrabold flex items-center justify-center shadow-lg shadow-cyan-500/25 text-lg">
                  {s.n}
                </span>
                <div>
                  <h3 className="font-bold text-slate-900 mb-1.5">{s.title}</h3>
                  <p className="text-sm text-slate-500 leading-relaxed">{s.body}</p>
                </div>
              </div>
            </Reveal>
          ))}
        </div>
      </div>
    </section>
  )
}

/* ───────────────────────── pricing ───────────────────────── */

const ENTERPRISE_PLAN = {
  name: 'Enterprise', blurb: 'A yearly license for regulatory teams and CROs.',
  features: [
    'Unlimited protocols across your organization',
    'Exact TOST + RSABE + NTI sample-size methods',
    'Multi-guideline support (FDA / EMA / ICH M13A)',
    'Team seats with role-based access (author / reviewer / approver)',
    'Shared organization workspace with a self-serve join link',
    'Per-section regeneration, source tracing & notes',
    'Custom guideline templates',
    'Dedicated support & onboarding',
  ],
  cta: 'Contact sales',
}

function Pricing() {
  const p = ENTERPRISE_PLAN
  const [contactOpen, setContactOpen] = useState(false)
  return (
    <section id="pricing" className="bg-slate-50 py-24">
      <div className="max-w-6xl mx-auto px-5">
        <Reveal>
          <p className="text-center text-sm font-bold text-cyan-600 uppercase tracking-widest mb-3">Pricing</p>
          <h2 className="text-center text-3xl sm:text-4xl font-extrabold text-slate-900 tracking-tight">One plan, built for teams</h2>
          <p className="text-center text-slate-500 mt-4 text-lg">Quark AI is sold as an annual enterprise license — negotiated to fit your team.</p>
        </Reveal>

        <div className="mt-12 max-w-xl mx-auto">
          <Reveal>
            <div className="card-lift relative rounded-3xl p-10 flex flex-col bg-[#0B1E3D] text-white shadow-2xl shadow-cyan-900/30 border border-cyan-400/30">
              <span className="absolute top-6 right-6 text-[11px] font-bold uppercase tracking-wider text-cyan-300 bg-cyan-400/15 border border-cyan-400/30 px-2.5 py-1 rounded-full">
                Annual license
              </span>
              <h3 className="text-xl font-bold text-white">{p.name}</h3>
              <p className="text-sm mt-1 text-slate-300">{p.blurb}</p>
              <div className="mt-6 mb-6 flex items-end gap-1.5">
                <span className="text-4xl font-extrabold text-white">Custom pricing</span>
              </div>
              <ul className="space-y-3 mb-8 flex-1">
                {p.features.map(f => (
                  <li key={f} className="flex items-start gap-2.5 text-sm text-slate-200">
                    <span className="mt-0.5 flex-shrink-0 text-cyan-300">{Icon.check}</span>
                    {f}
                  </li>
                ))}
              </ul>
              <button
                onClick={() => setContactOpen(true)}
                className="lift w-full py-3 rounded-xl font-bold text-sm cursor-pointer bg-gradient-to-r from-cyan-500 to-teal-500 text-white shadow-lg shadow-cyan-500/30"
              >
                {p.cta}
              </button>
            </div>
          </Reveal>
        </div>
      </div>
      {contactOpen && <ContactSalesModal onClose={() => setContactOpen(false)} />}
    </section>
  )
}

/* ───────────────────────── FAQ ───────────────────────── */

const FAQS = [
  { q: 'Does it fabricate PK values?', a: 'No. PK parameters are extracted from real PubMed abstracts and DailyMed labels, each tagged with its source PMID. If the literature lacks a value, the within-subject CV falls back to a clearly flagged assumption rather than an invented number.' },
  { q: 'How is the sample size calculated?', a: 'With an exact two one-sided tests (TOST) engine using the non-central t distribution — validated against the PowerTOST reference. It handles average bioequivalence, reference-scaled ABE for highly-variable drugs, and tightened NTI limits.' },
  { q: 'Which study types are supported?', a: 'Small-molecule bioequivalence (BE) and biologic biosimilar PK. Biosimilar protocols automatically include a mandatory immunogenicity section with tiered ADA/NAb testing.' },
  { q: 'Can I edit the generated protocol?', a: 'Yes. Every section is editable in a rich text editor, can be regenerated independently, and shows its literature sources. The computed sample size stays locked as the single source of truth across all sections.' },
  { q: 'What can I export?', a: 'Formatted DOCX and PDF, including cover page, abbreviations, schedule of activities, sample-size determination tables, and a regulatory reference list appropriate to the study type.' },
  { q: 'Is this a regulatory submission?', a: 'Quark AI produces high-quality drafts for investigational use. Every protocol should be reviewed and signed off by a qualified clinical pharmacologist before any regulatory use.' },
]

function FAQ() {
  const [open, setOpen] = useState<number | null>(0)
  return (
    <section id="faq" className="bg-white py-24">
      <div className="max-w-3xl mx-auto px-5">
        <Reveal>
          <p className="text-center text-sm font-bold text-cyan-600 uppercase tracking-widest mb-3">FAQ</p>
          <h2 className="text-center text-3xl sm:text-4xl font-extrabold text-slate-900 tracking-tight mb-12">Questions, answered</h2>
        </Reveal>
        <div className="space-y-3">
          {FAQS.map((f, i) => {
            const isOpen = open === i
            return (
              <Reveal key={f.q} delay={i * 50}>
                <div className={`rounded-2xl border transition-colors ${isOpen ? 'border-cyan-200 bg-cyan-50/40' : 'border-slate-200 bg-white hover:border-slate-300'}`}>
                  <button
                    onClick={() => setOpen(isOpen ? null : i)}
                    className="w-full flex items-center justify-between gap-4 px-5 py-4 text-left cursor-pointer"
                    aria-expanded={isOpen}
                  >
                    <span className="font-semibold text-slate-900">{f.q}</span>
                    <span className={`flex-shrink-0 text-cyan-600 transition-transform duration-300 ${isOpen ? 'rotate-180' : ''}`}>{Icon.chevron}</span>
                  </button>
                  <div className={`acc-body ${isOpen ? 'open' : ''}`}>
                    <div>
                      <p className="px-5 pb-5 text-[15px] text-slate-600 leading-relaxed">{f.a}</p>
                    </div>
                  </div>
                </div>
              </Reveal>
            )
          })}
        </div>
      </div>
    </section>
  )
}

/* ───────────────────────── final CTA ───────────────────────── */

function FinalCTA({ onLaunch }: { onLaunch: () => void }) {
  return (
    <section className="bg-slate-50 py-24">
      <div className="max-w-5xl mx-auto px-5">
        <Reveal>
          <div className="relative overflow-hidden rounded-3xl px-8 py-16 text-center" style={{ background: 'radial-gradient(120% 120% at 50% 0%, #143063 0%, #0B1E3D 60%)' }}>
            <div className="absolute inset-0 hero-grid opacity-60" />
            <div className="absolute -top-16 left-1/2 -translate-x-1/2 w-96 h-96 rounded-full blob" style={{ background: 'radial-gradient(circle, rgba(34,211,238,0.25), transparent 60%)' }} />
            <div className="relative">
              <h2 className="text-3xl sm:text-4xl font-extrabold text-white tracking-tight">Draft your first protocol today</h2>
              <p className="mt-4 text-slate-300 text-lg max-w-xl mx-auto">From drug name to a complete, statistically-sound, export-ready draft — in under two minutes.</p>
              <button
                onClick={onLaunch}
                className="lift group mt-8 inline-flex items-center gap-2 px-7 py-4 rounded-2xl text-base font-bold text-white cursor-pointer bg-gradient-to-r from-cyan-500 to-teal-500 shadow-xl shadow-cyan-500/30"
              >
                Get started for free
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

function Footer() {
  return (
    <footer className="bg-[#0B1E3D] text-slate-400">
      <div className="max-w-6xl mx-auto px-5 py-14 grid grid-cols-2 md:grid-cols-4 gap-8">
        <div className="col-span-2 md:col-span-1">
          <div className="flex items-center gap-2.5 mb-3">
            <span className="w-9 h-9 rounded-xl bg-cyan-500/15 border border-cyan-400/30 flex items-center justify-center text-cyan-300">{Icon.logo}</span>
            <span className="text-white font-extrabold tracking-tight">Quark <span className="text-cyan-300">AI</span></span>
          </div>
          <p className="text-sm leading-relaxed">AI-assisted, statistically-rigorous FDA-compliant protocol generation.</p>
        </div>
        {[
          { h: 'Product', links: [{ l: 'Features', href: '#features' }, { l: 'Pricing', href: '#pricing' }, { l: 'How it works', href: '#how' }, { l: 'FAQ', href: '#faq' }] },
          { h: 'Company', links: [{ l: 'Contact sales', href: '#pricing' }] },
          { h: 'Legal', links: [{ l: 'Privacy', href: '/privacy' }, { l: 'Terms', href: '/terms' }] },
        ].map(col => (
          <div key={col.h}>
            <p className="text-xs font-bold text-white uppercase tracking-widest mb-4">{col.h}</p>
            <ul className="space-y-2.5">
              {col.links.map(({ l, href }) => (
                <li key={l}>
                  {href.startsWith('/') ? (
                    <Link to={href} className="text-sm hover:text-cyan-300 transition-colors">{l}</Link>
                  ) : (
                    <a href={href} className="text-sm hover:text-cyan-300 transition-colors">{l}</a>
                  )}
                </li>
              ))}
            </ul>
          </div>
        ))}
      </div>
      <div className="border-t border-white/8">
        <div className="max-w-6xl mx-auto px-5 py-6 flex flex-col sm:flex-row items-center justify-between gap-3 text-xs">
          <p>© {new Date().getFullYear()} Quark AI. For investigational use only.</p>
          <p>FDA BE 2014 · ICH E6(R2) GCP · 21 CFR Part 11</p>
        </div>
      </div>
    </footer>
  )
}

/* ───────────────────────── page ───────────────────────── */

export default function Landing() {
  const { login, signup } = useLaunch()

  return (
    <div className="bg-slate-50 overflow-x-hidden">
      <Nav onLogin={login} onSignup={signup} />
      <Hero onLaunch={signup} />
      <TrustMarquee />
      <Stats />
      <Features />
      <HowItWorks />
      <Pricing />
      <FAQ />
      <FinalCTA onLaunch={signup} />
      <Footer />
    </div>
  )
}
