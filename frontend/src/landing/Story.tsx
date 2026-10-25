import type { ReactNode } from 'react'
import { STORY_NOTE, STORY_STAGES } from './content'
import { BudgetPanel, ConfirmPanel, DescribePanel, EnginesPanel, ProtocolPanel } from './StoryPanels'

const PANELS: ReactNode[] = [
  <DescribePanel key="describe" />,
  <ConfirmPanel key="confirm" />,
  <EnginesPanel key="engines" />,
  <ProtocolPanel key="protocol" />,
  <BudgetPanel key="budget" />,
]

/**
 * The pinned story: a study being built, in five stages (describe, confirm,
 * engines, protocol, budget). Folds the old "Talk it through" section in.
 *
 * One DOM, three presentations (see landing.css):
 *   - pinned (>= 1024px, motion allowed): a sticky 100vh stage; scroll progress
 *     drives the headline swap, the panel build and the progress pill
 *   - stacked (phones and tablets): each stage fades up and builds once as it enters
 *   - static (reduced motion / no engine): a plain list, all text visible
 * Every stage's headline, copy and panel text is real DOM text in all three.
 * Every number in the panels is an example, and the section says so.
 */
export function Story() {
  const total = STORY_STAGES.length
  return (
    <section id="chat" aria-labelledby="story-heading" className="lp-story relative">
      <div className="lp-story__track">
        <div className="lp-story__sticky" data-stage="1">
          <h2 id="story-heading" className="lp-eyebrow lp-story__eyebrow">
            How a study gets built
          </h2>

          {STORY_STAGES.map((s, i) => (
            <div key={s.n} className="lp-stage" data-stage={s.n}>
              <div className="lp-stage__copy">
                <p className="lp-stage__num">
                  Step {s.n} of {total}
                </p>
                <h3 className="lp-stage__title">{s.title}</h3>
                <p className="lp-stage__body">{s.body}</p>
              </div>
              <div
                className="lp-stage__panel"
                role="group"
                aria-label={`Illustration for step ${s.n}: ${s.title.replace(/\.$/, '')}`}
              >
                {PANELS[i]}
              </div>
            </div>
          ))}

          <div aria-hidden className="lp-story__prog">
            <span className="lp-story__bar">
              <span className="lp-story__fill" />
            </span>
            <span>Scroll to build</span>
            <span className="lp-story__step tabular-nums">1 / {total}</span>
          </div>

          <div aria-hidden className="lp-frame">
            <div className="lp-frame__bar">
              <span className="lp-frame__dot" />
              <span className="lp-frame__dot" />
              <span className="lp-frame__dot" />
              <span className="lp-frame__title">QuarkClinical &middot; New study</span>
            </div>
          </div>

          <p className="lp-story__note">{STORY_NOTE}</p>
        </div>
      </div>
    </section>
  )
}
