import type { ReactNode } from 'react'
import QuarkLogo from '../components/QuarkLogo'
import { Badge } from '../ui'
import { BUDGET_LINES, BUDGET_TOTAL, PROTOCOL_SECTIONS, STORY_PROMPT } from './content'
import { ArrowUp, Check, Lock } from './icons'

/**
 * The five panels of the pinned story. Static look-alikes of the real product
 * UI (nothing here is focusable or clickable) with EXAMPLE values: the page
 * labels the whole story "Illustration - example values".
 *
 * The DOM always holds the final text. The motion engine (motion/story.ts)
 * finds these class names (.p1-*, .p2-*, ...) and rebuilds the state over
 * scroll: typing, cards, numbers locking, sections stacking, the total
 * counting. With no engine (reduced motion, phones' fallback, tests) the
 * panels simply show their finished state.
 */

function Label({ children }: { children: ReactNode }) {
  return <span className="lp-plabel">{children}</span>
}

/** A cyan "locked" tick: circle + check. The path is drawn by the engine. */
function LockTick({ className = '' }: { className?: string }) {
  return (
    <svg viewBox="0 0 20 20" aria-hidden focusable="false" className={`h-5 w-5 flex-none ${className}`} fill="none">
      <circle cx="10" cy="10" r="9" fill="rgb(34 211 238 / 0.14)" stroke="#22d3ee" strokeWidth="1.2" />
      <path
        className="p3-tickpath"
        d="M6 10.4l2.6 2.6L14 7.4"
        stroke="#67e8f9"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
        pathLength={1}
      />
    </svg>
  )
}

/* ───────────── 1. describe ───────────── */

export function DescribePanel() {
  return (
    <div className="lp-pcol flex h-full flex-col gap-5">
      <div className="p1-bubble flex items-start gap-3">
        <span className="mt-0.5 flex h-8 w-8 flex-none items-center justify-center rounded-full border border-white/15 bg-white/[0.06] text-white">
          <QuarkLogo size={17} />
        </span>
        <p className="m-0 max-w-[26rem] text-[15px] leading-6 text-fg">
          What study are you planning? Name the drug and describe it in plain language.
        </p>
      </div>

      <div className="p1-composer rounded-[22px] border border-white/[0.16] bg-white/[0.06] p-3.5 shadow-[0_8px_32px_-8px_rgba(0,0,0,0.55)]">
        <p aria-hidden className="m-0 min-h-[4.5rem] text-[16px] leading-6 text-fg">
          <span className="p1-typed">{STORY_PROMPT}</span>
          <span className="lp-caret" />
        </p>
        <span className="sr-only">{STORY_PROMPT}</span>
        <div className="mt-2 flex items-center justify-between gap-3">
          <span className="p1-modes inline-flex rounded-pill bg-white/10 p-[3px] text-[12px] font-medium ring-1 ring-inset ring-white/10">
            <span className="rounded-pill bg-white/[0.16] px-2.5 py-1 text-fg">Protocol</span>
            <span className="px-2.5 py-1 text-fg-muted">Budget</span>
          </span>
          <span className="p1-send inline-flex h-9 w-9 items-center justify-center rounded-full bg-white text-ink-950">
            <ArrowUp aria-hidden className="h-4 w-4" />
          </span>
        </div>
      </div>
    </div>
  )
}

/* ───────────── 2. confirm ───────────── */

function StudyTypeOption({
  title,
  detail,
  selected = false,
}: {
  title: string
  detail: string
  selected?: boolean
}) {
  return (
    <li
      className={`p2-opt relative flex items-center gap-3 rounded-2xl border px-3.5 py-2.5 ${
        selected ? 'border-white/10' : 'p2-na border-white/10 opacity-[0.38]'
      }`}
    >
      {selected ? (
        <span aria-hidden className="p2-sel absolute inset-0 rounded-2xl border border-accent-300/70 bg-accent-300/[0.09]" />
      ) : null}
      {selected ? (
        <span className="p2-check relative flex h-5 w-5 flex-none items-center justify-center rounded-full bg-accent-300 text-ink-950">
          <Check aria-hidden className="h-3 w-3" strokeWidth={3} />
        </span>
      ) : (
        <span aria-hidden className="relative h-5 w-5 flex-none rounded-full border border-white/25" />
      )}
      <span className="relative min-w-0 flex-1">
        <span className="block text-[14px] font-semibold leading-5 text-fg">{title}</span>
        <span className="block text-[12px] leading-4 text-fg-muted">{detail}</span>
      </span>
    </li>
  )
}

export function ConfirmPanel() {
  return (
    <div className="lp-pcol flex h-full flex-col gap-3.5">
      <div className="p2-card lp-pcard">
        <div className="flex items-center justify-between gap-3">
          <Label>Drug match</Label>
          <Badge variant="success" icon={<Check aria-hidden />} className="p2-confirmed">
            Confirmed
          </Badge>
        </div>
        <div className="mt-2.5 flex flex-wrap items-center gap-2">
          <span className="mr-1 text-[1.5rem] font-semibold leading-8 tracking-[-0.03em] text-fg">adalimumab</span>
          <Badge variant="info" className="p2-chip">
            Biologic
          </Badge>
          <Badge variant="neutral" className="p2-chip">
            Label found
          </Badge>
          <Badge variant="neutral" className="p2-chip">
            Prior trials found
          </Badge>
        </div>
      </div>

      <div className="p2-card lp-pcard">
        <div className="flex items-center justify-between gap-3">
          <Label>Study type</Label>
          <Badge variant="success" icon={<Check aria-hidden />} className="p2-confirmed">
            Confirmed
          </Badge>
        </div>
        <ul className="m-0 mt-2.5 flex list-none flex-col gap-2 p-0">
          <StudyTypeOption selected title="Biosimilar PK" detail="Similarity of PK to the reference biologic" />
          <StudyTypeOption title="Bioequivalence" detail="Not applicable to biologics" />
          <StudyTypeOption title="Food effect" detail="Not applicable to biologics" />
        </ul>
      </div>
    </div>
  )
}

/* ───────────── 3. engines ───────────── */

function EngineRow({
  label,
  detail,
  children,
}: {
  label: string
  detail: string
  children: ReactNode
}) {
  return (
    <div className="p3-row lp-pcard flex items-center gap-4">
      <div className="min-w-0 flex-1">
        <Label>{label}</Label>
        <p className="m-0 mt-1 text-[1.1875rem] font-semibold leading-7 tracking-[-0.03em] text-fg tabular-nums sm:text-[1.375rem]">
          {children}
        </p>
        <p className="m-0 mt-0.5 text-[12px] leading-4 text-fg-muted">{detail}</p>
      </div>
      <div className="flex flex-none items-center gap-2">
        <span className="p3-locked text-[12px] font-semibold text-accent-300">Locked</span>
        <span className="p3-tick inline-flex">
          <LockTick />
        </span>
      </div>
    </div>
  )
}

export function EnginesPanel() {
  return (
    <div className="lp-pcol flex h-full flex-col gap-3">
      <div className="p3-head flex items-center justify-between gap-3">
        <Label>Computed by engines</Label>
        <Badge variant="neutral" dot>
          Locked once
        </Badge>
      </div>
      <EngineRow label="Sample size" detail="Exact TOST, 90% power, CV 30%">
        <span className="p3-num" data-to="32">
          32
        </span>{' '}
        <span className="text-[15px] font-medium tracking-normal text-fg-muted">per arm</span>
      </EngineRow>
      <EngineRow label="PK sampling schedule" detail="Dense around Tmax, sparse in the terminal phase">
        <span className="p3-num" data-to="18">
          18
        </span>{' '}
        <span className="text-[15px] font-medium tracking-normal text-fg-muted">samples over</span>{' '}
        <span className="p3-num" data-to="70">
          70
        </span>{' '}
        <span className="text-[15px] font-medium tracking-normal text-fg-muted">days</span>
      </EngineRow>
      <EngineRow label="Design decisions" detail="Subcutaneous, prefilled syringe, abdomen">
        Parallel, single dose
      </EngineRow>
    </div>
  )
}

/* ───────────── 4. protocol ───────────── */

export function ProtocolPanel() {
  const total = PROTOCOL_SECTIONS.length
  const perColumn = Math.ceil(total / 2)
  return (
    <div className="lp-pcol flex h-full flex-col gap-3">
      <div className="p4-head flex items-center justify-between gap-3">
        <Label>Protocol draft</Label>
        <span className="text-[13px] font-semibold text-fg tabular-nums">
          <span className="p4-count" data-to={total}>
            {total}
          </span>{' '}
          <span className="font-medium text-fg-muted">of {total} sections</span>
        </span>
      </div>
      <ol
        className="lp-p4-list m-0 list-none p-0"
        style={{ ['--lp-rows' as string]: perColumn }}
      >
        {PROTOCOL_SECTIONS.map((s, i) => (
          <li
            key={s.title}
            className="p4-row flex min-h-[2.125rem] items-center gap-2 rounded-xl border border-white/[0.07] bg-white/[0.04] px-2.5 py-1"
          >
            <span aria-hidden className="w-4 flex-none text-[10px] font-semibold text-fg-subtle tabular-nums">
              {String(i + 1).padStart(2, '0')}
            </span>
            <span className="min-w-0 flex-1 text-[12px] font-medium leading-[1.15rem] text-fg">{s.title}</span>
            {s.locked ? (
              <span className="p4-lock inline-flex flex-none items-center gap-1 rounded-pill bg-accent-300/[0.12] px-1.5 py-0.5 text-[10px] font-semibold text-accent-300">
                <Lock aria-hidden className="h-2.5 w-2.5" />
                <span className="sr-only">Uses locked numbers</span>
                <span aria-hidden className="hidden xl:inline">
                  locked
                </span>
              </span>
            ) : (
              <Check aria-hidden className="h-3.5 w-3.5 flex-none text-accent-300/70" />
            )}
          </li>
        ))}
      </ol>
    </div>
  )
}

/* ───────────── 5. budget ───────────── */

const usd = (n: number) => `$${n.toLocaleString('en-US')}`

export function BudgetPanel() {
  const max = Math.max(...BUDGET_LINES.map(l => l.amount))
  return (
    <div className="lp-pcol flex h-full flex-col gap-3.5">
      <div className="p5-head flex items-end justify-between gap-3">
        <div>
          <Label>Study budget, estimate</Label>
          <p className="m-0 mt-1 text-[2.5rem] font-bold leading-10 tracking-[-0.045em] text-fg tabular-nums">
            $
            <span className="p5-total" data-to={BUDGET_TOTAL}>
              {BUDGET_TOTAL.toLocaleString('en-US')}
            </span>
          </p>
        </div>
        <Badge variant="neutral">Example values</Badge>
      </div>
      <ul className="m-0 flex list-none flex-col gap-2.5 p-0">
        {BUDGET_LINES.map(l => (
          <li key={l.label} className="p5-row">
            <div className="flex items-baseline justify-between gap-3">
              <span className="text-[13px] font-medium text-fg">{l.label}</span>
              <span className="text-[13px] font-semibold text-fg tabular-nums">{usd(l.amount)}</span>
            </div>
            <div aria-hidden className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-white/10">
              <div
                className="p5-bar h-full rounded-full bg-gradient-to-r from-accent-400 to-accent-300"
                style={{ width: `${Math.round((l.amount / max) * 100)}%` }}
              />
            </div>
          </li>
        ))}
      </ul>
      <p className="p5-note m-0 text-[12px] leading-[1.15rem] text-fg-muted">
        Enrollment and sites come from the drug&rsquo;s own registered trials; rates come from a documented rate
        card. An estimate to compare against CRO quotes, not a quote.
      </p>
    </div>
  )
}
