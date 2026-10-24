import type { ComponentType, SVGProps } from 'react'
import { WHY, type WhyIconKey } from './content'
import { Reveal, SectionHeading } from './Reveal'
import { ChartColumn, FlaskConical, Lock, Shield, Stethoscope, Users } from './icons'

const ICONS: Record<WhyIconKey, ComponentType<SVGProps<SVGSVGElement>>> = {
  chart: ChartColumn,
  beaker: FlaskConical,
  stethoscope: Stethoscope,
  lock: Lock,
  users: Users,
  shield: Shield,
}

export function Why() {
  return (
    <section id="why" aria-labelledby="why-heading" className="scroll-mt-24 bg-surface py-24">
      <div className="mx-auto max-w-6xl px-5">
        <Reveal>
          <SectionHeading
            id="why-heading"
            eyebrow="Why QuarkClinical"
            title="The numbers are computed, not generated"
            lead="The language model writes the prose. Everything a regulator would check is calculated by code first and handed to it as fixed facts."
          />
        </Reveal>

        <ul className="m-0 mt-14 grid list-none gap-5 p-0 sm:grid-cols-2 lg:grid-cols-3">
          {WHY.map((f, i) => {
            const Icon = ICONS[f.icon]
            return (
              <li key={f.title} className="flex">
                <Reveal delay={(i % 3) * 90} className="flex w-full">
                  <article className="flex w-full flex-col rounded-card border border-line bg-surface-canvas p-6">
                    <span
                      aria-hidden
                      className="mb-4 inline-flex h-11 w-11 items-center justify-center rounded-xl border border-line bg-surface text-accent-700"
                    >
                      <Icon className="h-5 w-5" />
                    </span>
                    <h3 className="mb-2 text-h3 text-fg">{f.title}</h3>
                    <p className="m-0 text-body text-fg-muted">{f.body}</p>
                  </article>
                </Reveal>
              </li>
            )
          })}
        </ul>
      </div>
    </section>
  )
}
