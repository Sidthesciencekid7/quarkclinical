import type { ReactNode } from 'react'
import QuarkLogo from '../components/QuarkLogo'
import { Badge, ChatCard, buttonClasses } from '../ui'
import { Eyebrow, Reveal } from './Reveal'
import { Calculator, Check, FlaskConical } from './icons'

const POINTS = [
  'It asks only for what is missing.',
  'Every number sits in a card with its reasoning and source.',
  'You confirm each step; nothing is generated until you do.',
]

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-4 border-b border-line py-2 last:border-b-0">
      <dt className="text-small text-fg-muted">{label}</dt>
      <dd className="m-0 text-right text-small font-medium text-fg">{children}</dd>
    </div>
  )
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-xl bg-surface-muted px-3 py-2">
      <dt className="text-[11px] font-medium uppercase tracking-[0.06em] text-fg-muted">{label}</dt>
      <dd className="m-0 mt-0.5 text-body font-semibold tabular-nums text-fg">{value}</dd>
    </div>
  )
}

// Static look-alikes of the real card buttons. They are spans, not buttons,
// so the illustration contains nothing focusable or clickable.
const GHOST_BTN = buttonClasses({ variant: 'secondary', size: 'sm' })
const PRIMARY_BTN = buttonClasses({ variant: 'primary', size: 'sm' })

/**
 * STATIC illustration of a chat exchange, built from the same card markup the
 * app uses. It is labelled as an illustration with example values: nothing in
 * it is a real study, and nothing in it is interactive.
 */
function ChatIllustration() {
  return (
    <figure className="m-0">
      <div
        role="img"
        aria-label="Illustration of a chat. A user describes a biosimilar PK study of adalimumab in healthy volunteers. The assistant replies with a study design card and a sample size card, each waiting for confirmation."
        className="overflow-hidden rounded-card border border-line bg-surface shadow-pop"
      >
        <div className="flex items-center justify-between gap-3 border-b border-line bg-surface-muted px-4 py-2.5">
          <span className="flex items-center gap-2 text-small font-medium text-fg">
            <span aria-hidden className="h-2 w-2 rounded-full bg-accent-600" />
            New study
          </span>
          <Badge variant="neutral">Illustration, example values</Badge>
        </div>

        <div aria-hidden className="flex flex-col gap-4 p-4 sm:p-5">
          {/* user */}
          <div className="ml-auto max-w-[88%] rounded-2xl rounded-br-md bg-ink-950 px-4 py-2.5 text-body text-white">
            Biosimilar PK study for adalimumab in healthy volunteers
          </div>

          {/* assistant text */}
          <div className="flex items-start gap-3">
            <span className="mt-0.5 flex h-7 w-7 flex-none items-center justify-center rounded-full border border-line bg-surface-muted text-fg">
              <QuarkLogo size={16} />
            </span>
            <p className="m-0 max-w-[92%] text-body text-fg">
              Adalimumab is a biologic, so I have set this up as a biosimilar PK study. Here is the design the
              engines propose. Change anything that does not fit.
            </p>
          </div>

          {/* design card */}
          <ChatCard
            icon={<FlaskConical />}
            title="Study design"
            status={<Badge variant="info">Needs your confirmation</Badge>}
            actions={
              <>
                <span className={GHOST_BTN}>Change</span>
                <span className={PRIMARY_BTN}>Confirm design</span>
              </>
            }
          >
            <dl className="m-0">
              <Row label="Population">Healthy volunteers</Row>
              <Row label="Design">Parallel, single dose</Row>
              <Row label="Route">Subcutaneous, prefilled syringe, abdomen</Row>
            </dl>
            <p className="mb-0 mt-3 text-small text-fg-muted">
              Why: a long half-life makes a crossover washout impractical, so each subject receives one product.
            </p>
          </ChatCard>

          {/* sample size card */}
          <ChatCard
            icon={<Calculator />}
            title="Sample size"
            status={
              <Badge variant="success" icon={<Check />}>
                Computed by the engine
              </Badge>
            }
          >
            <dl className="m-0 grid grid-cols-3 gap-2">
              <Stat label="Per arm" value="32" />
              <Stat label="Power" value="90%" />
              <Stat label="CV" value="30%" />
            </dl>
            <p className="mb-0 mt-3 text-small text-fg-muted">
              Exact TOST on the non-central t distribution. The CV and its literature source are shown on the card.
            </p>
          </ChatCard>

          {/* assistant text */}
          <div className="flex items-start gap-3">
            <span className="mt-0.5 flex h-7 w-7 flex-none items-center justify-center rounded-full border border-line bg-surface-muted text-fg">
              <QuarkLogo size={16} />
            </span>
            <p className="m-0 max-w-[92%] text-body text-fg">
              Happy with this? Confirm the design and I will draft the protocol. Nothing is generated until you do.
            </p>
          </div>
        </div>
      </div>
      <figcaption className="mt-3 text-center text-small text-fg-subtle">
        Illustration with example values, not a real study.
      </figcaption>
    </figure>
  )
}

export function TalkItThrough() {
  return (
    <section id="chat" aria-labelledby="chat-heading" className="scroll-mt-24 bg-surface-canvas py-24">
      <div className="mx-auto max-w-6xl px-5">
        <div className="grid items-center gap-12 lg:grid-cols-2 lg:gap-16">
          <Reveal>
            <Eyebrow>Chat-first</Eyebrow>
            <h2
              id="chat-heading"
              className="mt-3 text-[1.75rem] font-semibold leading-tight tracking-[-0.025em] text-fg sm:text-[2.25rem]"
            >
              Talk it through. Check every number.
            </h2>
            <p className="mt-4 text-[1.0625rem] leading-relaxed text-fg-muted">
              You describe the study in plain language. QuarkClinical asks for what it needs, hands the design to
              tested engines, and shows every number in a card with its reasoning and source. Nothing is generated
              until you confirm it.
            </p>
            <ul className="m-0 mt-6 flex list-none flex-col gap-3 p-0">
              {POINTS.map(pt => (
                <li key={pt} className="flex items-start gap-2.5 text-body text-fg-muted">
                  <Check aria-hidden className="mt-1 h-4 w-4 flex-none text-accent-700" />
                  <span>{pt}</span>
                </li>
              ))}
            </ul>
          </Reveal>

          <Reveal delay={120}>
            <ChatIllustration />
          </Reveal>
        </div>
      </div>
    </section>
  )
}
