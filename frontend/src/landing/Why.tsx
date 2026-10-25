import type { ComponentType, SVGProps } from 'react'
import { WHY, type WhyIconKey } from './content'
import { SectionHeading, Stagger, StaggerItem } from './Reveal'
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
    <section id="why" aria-labelledby="why-heading" className="relative scroll-mt-20 py-28 sm:py-36">
      <div aria-hidden className="lp-hair absolute inset-x-0 top-0" />
      <div className="mx-auto max-w-6xl px-5">
        <SectionHeading
          id="why-heading"
          eyebrow="Why QuarkClinical"
          title="The numbers are computed, not generated."
          lead="The language model writes the prose. Everything a regulator would check is calculated by code first and handed to it as fixed facts."
        />

        <Stagger as="ul" className="m-0 mt-16 grid list-none gap-5 p-0 sm:grid-cols-2 lg:grid-cols-3">
          {WHY.map(f => {
            const Icon = ICONS[f.icon]
            return (
              <StaggerItem as="li" key={f.title} className="flex">
                <article className="lp-card lp-lift flex w-full flex-col p-7">
                  <span
                    aria-hidden
                    className="mb-6 inline-flex h-11 w-11 items-center justify-center rounded-xl border border-line bg-white/[0.05] text-accent-300"
                  >
                    <Icon className="h-5 w-5" />
                  </span>
                  <h3 className="lp-title mb-3 !text-[1.375rem]">{f.title}</h3>
                  <p className="m-0 text-body text-fg-muted">{f.body}</p>
                </article>
              </StaggerItem>
            )
          })}
        </Stagger>
      </div>
    </section>
  )
}
