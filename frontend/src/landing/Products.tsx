import { PRODUCTS } from './content'
import { ProductIllustration } from './illustrations'
import { ProductShot } from './ProductShot'
import { Eyebrow, Reveal, SectionHeading, Stagger, StaggerItem } from './Reveal'
import { Check } from './icons'

export function Products() {
  return (
    <section id="products" aria-labelledby="products-heading" className="relative scroll-mt-20 py-28 sm:py-36">
      <div aria-hidden className="lp-hair absolute inset-x-0 top-0" />
      <div className="mx-auto max-w-6xl px-5">
        <SectionHeading
          id="products-heading"
          eyebrow="One workspace"
          title="Design it, cost it, report it."
          lead="Three tools that share one study, so the protocol, the budget and the report stay consistent."
        />

        <Stagger as="ul" className="m-0 mt-16 grid list-none gap-5 p-0 md:grid-cols-3">
          {PRODUCTS.map(p => (
            <StaggerItem as="li" key={p.key} className="flex">
              <article className="lp-card lp-lift flex w-full flex-col p-3">
                <div className="flex aspect-[1.7/1] w-full items-center justify-center overflow-hidden rounded-[18px] bg-white/[0.04]">
                  <div className="h-full w-full p-3">
                    <ProductIllustration product={p.key} />
                  </div>
                </div>
                <div className="flex flex-1 flex-col px-3 pb-3 pt-6">
                  <h3 className="lp-title">{p.name}</h3>
                  <p className="m-0 mt-2 text-body text-fg-muted">{p.tagline}</p>
                  <ul className="m-0 mt-6 flex list-none flex-col gap-3 p-0">
                    {p.points.map(pt => (
                      <li key={pt} className="flex items-start gap-2.5 text-small text-fg-muted">
                        <Check aria-hidden className="mt-0.5 h-4 w-4 flex-none text-accent-300" />
                        <span>{pt}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              </article>
            </StaggerItem>
          ))}
        </Stagger>

        <div className="mt-28 grid items-center gap-12 lg:mt-36 lg:grid-cols-5 lg:gap-16">
          <div className="lg:col-span-2">
            <Reveal>
              <Eyebrow accent>Budgeting Tool</Eyebrow>
            </Reveal>
            <h3 data-lp="split" className="lp-h2 mt-4 !text-[clamp(2rem,4vw,3.25rem)]">
              A budget you can defend line by line.
            </h3>
            <Reveal delay={0.25}>
              <p className="lp-lead mt-6">
                Type a drug name and get a complete study budget. Each line shows how its quantity was derived and
                where its rate came from, so you can check it against CRO quotes instead of starting from a blank
                sheet.
              </p>
            </Reveal>
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
      </div>
    </section>
  )
}
