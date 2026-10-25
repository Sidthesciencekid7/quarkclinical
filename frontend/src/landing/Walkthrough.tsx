import { Reveal } from './Reveal'

// Set to a YouTube/Vimeo embed URL once the walkthrough is recorded; the video
// section stays hidden until then (the page never ships an empty player).
export const DEMO_VIDEO_EMBED_URL: string = ''

export function Walkthrough() {
  if (!DEMO_VIDEO_EMBED_URL) return null
  return (
    <section id="walkthrough" aria-labelledby="walkthrough-heading" className="scroll-mt-20 py-28">
      <div className="mx-auto max-w-5xl px-5">
        <Reveal>
          <h2 id="walkthrough-heading" className="lp-h2 mb-12 text-center">
            See a study built end to end.
          </h2>
          <div className="lp-device relative aspect-video">
            <iframe
              src={DEMO_VIDEO_EMBED_URL}
              title="QuarkClinical walkthrough"
              className="absolute inset-0 h-full w-full"
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
