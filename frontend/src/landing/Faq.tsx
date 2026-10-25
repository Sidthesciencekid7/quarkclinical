import { useId, useState } from 'react'
import { FAQS } from './content'
import { SectionHeading } from './Reveal'
import { ChevronDown } from './icons'

/**
 * Accessible accordion: each question is a button (aria-expanded,
 * aria-controls) inside a heading, and its answer is a labelled region that is
 * fully hidden (not focusable, not announced) while collapsed. One answer is
 * open at a time; Enter / Space toggle natively because they are buttons.
 */
export function Faq({ items = FAQS, initialOpen = 0 }: { items?: { q: string; a: string }[]; initialOpen?: number | null }) {
  const [open, setOpen] = useState<number | null>(initialOpen)
  const baseId = useId()

  return (
    <section id="faq" aria-labelledby="faq-heading" className="relative scroll-mt-20 py-28 sm:py-36">
      <div aria-hidden className="lp-hair absolute inset-x-0 top-0" />
      <div className="mx-auto max-w-3xl px-5">
        <SectionHeading id="faq-heading" eyebrow="FAQ" title="Questions, answered." />

        <div data-lp="fade" className="mt-16 flex flex-col gap-3">
          {items.map((f, i) => {
            const isOpen = open === i
            const buttonId = `${baseId}-q${i}`
            const panelId = `${baseId}-a${i}`
            return (
              <div
                key={f.q}
                className={`rounded-[20px] border transition-colors duration-fast ${
                  isOpen ? 'border-line-strong bg-white/[0.06]' : 'border-line bg-white/[0.025] hover:border-line-strong'
                }`}
              >
                <h3 className="m-0">
                  <button
                    type="button"
                    id={buttonId}
                    aria-expanded={isOpen}
                    aria-controls={panelId}
                    onClick={() => setOpen(isOpen ? null : i)}
                    className="flex w-full cursor-pointer items-center justify-between gap-4 rounded-[20px] px-6 py-5 text-left text-[1.0625rem] font-semibold leading-6 tracking-[-0.01em] text-fg"
                  >
                    <span>{f.q}</span>
                    <ChevronDown
                      aria-hidden
                      className={`h-[18px] w-[18px] flex-none text-fg-muted transition-transform duration-panel ${
                        isOpen ? 'rotate-180' : ''
                      }`}
                    />
                  </button>
                </h3>
                <div id={panelId} role="region" aria-labelledby={buttonId} data-open={isOpen} className="lp-acc">
                  <div>
                    <p className="m-0 px-6 pb-6 text-body text-fg-muted">{f.a}</p>
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
