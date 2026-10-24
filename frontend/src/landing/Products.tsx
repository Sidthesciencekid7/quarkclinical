import { PRODUCTS } from './content'
import { ProductIllustration } from './illustrations'
import { ProductShot } from './ProductShot'
import { Eyebrow, Reveal, SectionHeading } from './Reveal'
import { Check } from './icons'

export function Products() {
  return (
    <section id="products" aria-labelledby="products-heading" className="scroll-mt-24 bg-surface py-24">
      <div className="mx-auto max-w-6xl px-5">
        <Reveal>
          <SectionHeading
            id="products-heading"
            eyebrow="One workspace"
            title="Design it, cost it, report it"
            lead="Three tools that share one study, so the protocol, the budget and the report stay consistent."
          />
        </Reveal>

        <ul className="m-0 mt-14 grid list-none gap-5 p-0 md:grid-cols-3">
          {PRODUCTS.map((p, i) => (
            <li key={p.key} className="flex">
              <Reveal delay={i * 90} className="flex w-full">
                <article className="card lp-lift flex w-full flex-col p-3 hover:border-line-strong hover:shadow-pop">
                  <div className="tile aspect-[1.7/1] w-full">
                    <div className="h-full w-full p-3">
                      <ProductIllustration product={p.key} />
                    </div>
                  </div>
                  <div className="flex flex-1 flex-col px-3 pb-3 pt-5">
                    <h3 className="text-h3 text-fg">{p.name}</h3>
                    <p className="mt-1 text-body text-fg-muted">{p.tagline}</p>
                    <ul className="mt-5 flex list-none flex-col gap-2.5 p-0">
                      {p.points.map(pt => (
                        <li key={pt} className="flex items-start gap-2.5 text-small text-fg-muted">
                          <Check aria-hidden className="mt-0.5 h-4 w-4 flex-none text-accent-700" />
                          <span>{pt}</span>
                        </li>
                      ))}
                    </ul>
                  </div>
                </article>
              </Reveal>
            </li>
          ))}
        </ul>

        <Reveal className="mt-20">
          <div className="grid items-center gap-10 lg:grid-cols-5">
            <div className="lg:col-span-2">
              <Eyebrow>Budgeting Tool</Eyebrow>
              <h3 className="mt-3 text-[1.5rem] font-semibold leading-tight tracking-[-0.02em] text-fg sm:text-[1.875rem]">
                A budget you can defend line by line
              </h3>
              <p className="mt-4 text-[1.0625rem] leading-relaxed text-fg-muted">
                Type a drug name and get a complete study budget. Each line shows how its quantity was derived and
                where its rate came from, so you can check it against CRO quotes instead of starting from a blank
                sheet.
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
