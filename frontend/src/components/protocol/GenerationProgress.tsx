import { AlertTriangle, Check, Clock, Spinner, XCircle, cx } from '../../ui'
import type { GenerationPhase } from '../../lib/useGenerationPoll'

const STEPS = [
  { label: 'Searching PubMed for PK literature', sublabel: 'PubMed and the DailyMed label' },
  { label: 'Extracting pharmacokinetic parameters', sublabel: 'Structured extraction with PMID validation' },
  { label: 'Generating protocol sections', sublabel: 'Guideline-mapped, around the engines’ locked numbers' },
  { label: 'Saving the protocol', sublabel: 'With its version history and audit trail' },
] as const

// There is no server-side progress signal: the backend runs one long PK
// extraction call, one large generation call and (sometimes) a QC remediation
// pass, with no per-section reporting back to the client. So the percentage is
// a SHAPE, not a measurement: it exists to show visible motion, not to claim a
// real completion fraction. It is deliberately NOT surfaced as a time
// remaining, because a wrong ETA is worse than none.
const EXPECTED_GENERATION_SECONDS = 20 * 60

/** Asymptotic curve: quick early feedback, then slows and creeps toward a 96%
 *  soft ceiling. It NEVER reaches 100 on its own; only the real completion
 *  signal does. Real generations routinely run past the expected duration, so a
 *  linear bar would either finish "early" or overflow. */
export function estimateProgressPct(elapsedSec: number): number {
  const tau = EXPECTED_GENERATION_SECONDS * 0.6
  return Math.min(96, 96 * (1 - Math.exp(-elapsedSec / tau)))
}

export function formatDuration(ms: number): string {
  const totalSec = Math.max(0, Math.round(ms / 1000))
  const m = Math.floor(totalSec / 60)
  const s = totalSec % 60
  return `${m}:${s.toString().padStart(2, '0')}`
}

/** Which of the four steps to show as active, from time alone (cosmetic). */
export function stepForElapsed(elapsedMs: number, phase: GenerationPhase): number {
  if (phase === 'complete') return STEPS.length - 1
  if (elapsedMs >= 6500) return 2
  if (elapsedMs >= 2500) return 1
  return 0
}

export interface GenerationProgressProps {
  phase: GenerationPhase
  elapsedMs: number
  /** Server-supplied failure text for `phase === 'failed'`. */
  error?: string | null
  className?: string
}

/**
 * Progress UI for a protocol being generated (moved out of the retired
 * GenerateButton wizard step). `still_running` is amber, never red: nothing
 * failed, only this page stopped watching.
 */
export function GenerationProgress({ phase, elapsedMs, error, className }: GenerationProgressProps) {
  if (phase === 'failed') {
    return (
      <div role="alert" className={cx('flex items-start gap-3 rounded-input border px-4 py-3 text-small', 'border-[color:var(--bad-border)] bg-[var(--bad-bg)] text-[color:var(--bad-fg)]', className)}>
        <XCircle aria-hidden className="mt-0.5 h-4 w-4 flex-none" />
        <p>
          <span className="font-semibold">Generation failed:</span> {error || 'Generation failed. Please try again.'}
        </p>
      </div>
    )
  }

  if (phase === 'still_running') {
    return (
      <div role="status" className={cx('flex items-start gap-3 rounded-input border px-4 py-3 text-small', 'border-[color:var(--warn-border)] bg-[var(--warn-bg)] text-[color:var(--warn-fg)]', className)}>
        <Clock aria-hidden className="mt-0.5 h-4 w-4 flex-none" />
        <p>
          <span className="font-semibold">Still generating.</span> This is a long protocol and it has exceeded the time this page waits.
          It has <span className="font-semibold">not</span> failed: generation continues on the server and the finished protocol will appear
          under <span className="font-semibold">Protocols</span>. Open it from there once it finishes (it can take up to about half an hour); there is no need to generate again.
        </p>
      </div>
    )
  }

  const complete = phase === 'complete'
  const active = stepForElapsed(elapsedMs, phase)
  const pct = complete ? 100 : Math.max(4, estimateProgressPct(elapsedMs / 1000))

  return (
    <div className={cx('space-y-4', className)}>
      <p className="text-small text-fg-muted">
        {complete
          ? 'Generation finished.'
          : 'Real generations often run well past 15 minutes, which is normal. You can leave this page; the protocol keeps generating and will be in your Protocols list.'}
      </p>

      <div>
        <div className="mb-1.5 flex items-baseline justify-between">
          <span className="text-h3 tabular-nums text-fg">{Math.round(pct)}%</span>
          <span className="text-small tabular-nums text-fg-subtle">Watching for {formatDuration(elapsedMs)}</span>
        </div>
        <div
          role="progressbar"
          aria-label="Generation progress (indicative)"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={Math.round(pct)}
          className="h-1.5 w-full overflow-hidden rounded-full bg-surface-muted"
        >
          <div className="h-full rounded-full bg-accent-500 transition-all duration-1000 ease-out" style={{ width: `${pct}%` }} />
        </div>
      </div>

      <ol className="m-0 list-none space-y-3 p-0">
        {STEPS.map((step, idx) => {
          const done = complete || idx < active
          const current = !complete && idx === active
          return (
            <li key={step.label} className="flex items-start gap-3">
              <span className="mt-0.5 flex h-5 w-5 flex-none items-center justify-center">
                {done ? (
                  <span className="flex h-5 w-5 items-center justify-center rounded-full bg-[var(--ok-fg)] text-white">
                    <Check aria-hidden className="h-3 w-3" />
                  </span>
                ) : current ? (
                  <Spinner size={18} className="text-accent-600" />
                ) : (
                  <span className="flex h-5 w-5 items-center justify-center rounded-full border-2 border-line text-[10px] font-semibold text-fg-subtle">
                    {idx + 1}
                  </span>
                )}
              </span>
              <div>
                <p className={cx('text-small font-semibold leading-tight', done ? 'text-[color:var(--ok-fg)]' : current ? 'text-fg' : 'text-fg-subtle')}>
                  {step.label}
                </p>
                <p className={cx('mt-0.5 text-small', current ? 'text-fg-muted' : 'text-fg-subtle')}>{step.sublabel}</p>
              </div>
            </li>
          )
        })}
      </ol>
    </div>
  )
}

/** Small amber/red helper used by cards that need the same tones without the full progress UI. */
export function GenerationNote({ tone, children }: { tone: 'warn' | 'bad'; children: React.ReactNode }) {
  return (
    <div
      role={tone === 'bad' ? 'alert' : 'status'}
      className={cx(
        'flex items-start gap-2 rounded-input border px-3 py-2 text-small',
        tone === 'bad'
          ? 'border-[color:var(--bad-border)] bg-[var(--bad-bg)] text-[color:var(--bad-fg)]'
          : 'border-[color:var(--warn-border)] bg-[var(--warn-bg)] text-[color:var(--warn-fg)]',
      )}
    >
      <AlertTriangle aria-hidden className="mt-0.5 h-4 w-4 flex-none" />
      <div>{children}</div>
    </div>
  )
}

export default GenerationProgress
