import { Badge, Check, cx } from '../ui'
import type { BadgeVariant } from '../ui'
import type { FieldStatus, SpecField, SpecSummary } from './types'

const STATUS_BADGE: Record<FieldStatus, { label: string; variant: BadgeVariant }> = {
  confirmed: { label: 'Confirmed', variant: 'success' },
  proposed: { label: 'Proposed', variant: 'warning' },
  engine: { label: 'Engine', variant: 'info' },
  default: { label: 'Default', variant: 'neutral' },
  empty: { label: 'Empty', variant: 'neutral' },
}

function labelFor(spec: SpecSummary, path: string): string {
  return spec.fields.find((f: SpecField) => f.path === path)?.label ?? path.replace(/_/g, ' ')
}

export interface SpecPanelProps {
  spec: SpecSummary | null
  className?: string
  /** Show the "Study spec" heading (default). Hide it where a dialog already carries that title. */
  heading?: boolean
}

/**
 * The "Study spec": every field of the draft the assistant is building, with
 * where its value came from (confirmed by you, proposed from your text,
 * computed by an engine, or a default) and what is still missing. A read-only
 * mirror of the server's draft; the values come from the server's summary.
 */
export function SpecPanel({ spec, className, heading = true }: SpecPanelProps) {
  if (!spec) {
    return (
      <div className={className}>
        <p className="text-small text-fg-muted">The study spec appears here once the conversation starts.</p>
      </div>
    )
  }
  const missing = spec.missing.map(p => labelFor(spec, p))
  return (
    <div className={className} data-testid="spec-panel">
      <div className="mb-3 flex items-center justify-between gap-2">
        {heading ? <h2 className="text-h3 text-fg">Study spec</h2> : <span />}
        {spec.ready ? (
          <Badge variant="success" icon={<Check />}>
            Ready
          </Badge>
        ) : (
          <Badge variant="neutral">{spec.missing.length} to go</Badge>
        )}
      </div>

      {missing.length > 0 ? (
        <div className="mb-4 rounded-input border border-[color:var(--warn-border)] bg-[var(--warn-bg)] px-3 py-2 text-small text-[color:var(--warn-fg)]">
          <p className="font-semibold">Still needed</p>
          <ul className="m-0 mt-1 list-disc space-y-0.5 pl-4">
            {missing.map(m => (
              <li key={m}>{m}</li>
            ))}
          </ul>
        </div>
      ) : null}

      <dl className="m-0 divide-y divide-line">
        {spec.fields.map(f => {
          const b = STATUS_BADGE[f.status] ?? STATUS_BADGE.empty
          return (
            <div key={f.path} className="py-2.5">
              <dt className="flex items-center justify-between gap-2 text-small text-fg-subtle">
                <span className="min-w-0 truncate">{f.label}</span>
                <Badge variant={b.variant}>{b.label}</Badge>
              </dt>
              <dd className={cx('m-0 mt-0.5 break-words text-body', f.display_value ? 'font-medium text-fg' : 'text-fg-subtle')}>
                {f.display_value ?? 'Not set'}
              </dd>
            </div>
          )
        })}
      </dl>
    </div>
  )
}
