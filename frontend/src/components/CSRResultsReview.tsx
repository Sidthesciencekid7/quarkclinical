import type { ReactNode } from 'react'
import type { CsrResults } from '../types'
import { Badge, Card, CheckCircle2, Info, XCircle, cx } from '../ui'
import type { BadgeVariant } from '../ui'

/** Read-only display of the LOCKED, deterministically-computed CSR results:
 *  the bioequivalence verdict, the pivotal per-metric table, and subject
 *  disposition. These numbers are never editable: they are the analysis. */

const METRIC_LABEL: Record<string, string> = {
  cmax: 'Cmax',
  auc0t: 'AUC0-t',
  auc0inf: 'AUC0-∞',
}
const METRIC_ORDER = ['cmax', 'auc0t', 'auc0inf']

function pct(v?: number | null): string {
  return v === undefined || v === null || Number.isNaN(v) ? '—' : v.toFixed(2)
}

// Full literal class names (Tailwind scans for them).
const VERDICT: Record<string, { label: string; box: string; text: string; icon: ReactNode; badge: BadgeVariant }> = {
  bioequivalent: {
    label: 'Bioequivalent',
    box: 'border-[color:var(--ok-border)] bg-[var(--ok-bg)]',
    text: 'text-[color:var(--ok-fg)]',
    icon: <CheckCircle2 aria-hidden />,
    badge: 'success',
  },
  not_bioequivalent: {
    label: 'Not bioequivalent',
    box: 'border-[color:var(--bad-border)] bg-[var(--bad-bg)]',
    text: 'text-[color:var(--bad-fg)]',
    icon: <XCircle aria-hidden />,
    badge: 'danger',
  },
  indeterminate: {
    label: 'Indeterminate',
    box: 'border-[color:var(--warn-border)] bg-[var(--warn-bg)]',
    text: 'text-[color:var(--warn-fg)]',
    icon: <Info aria-hidden />,
    badge: 'warning',
  },
}

function Stat({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="rounded-input border border-line bg-surface px-4 py-3">
      <div className="eyebrow">{label}</div>
      <div className="mt-0.5 break-words text-body font-semibold text-fg">{value ?? '—'}</div>
    </div>
  )
}

export default function CSRResultsReview({ results }: { results: CsrResults }) {
  const be = results.be || {}
  const order = [
    ...METRIC_ORDER.filter(m => m in be),
    ...Object.keys(be).filter(m => !METRIC_ORDER.includes(m)),
  ]
  const disp = results.disposition || {}
  const v = VERDICT[results.verdict || 'indeterminate'] || VERDICT.indeterminate

  // Acceptance limits from the first analysable metric (default 80–125%).
  let lo = 80
  let hi = 125
  for (const m of order) {
    const e = be[m]
    if (e && !e.error && e.lower_limit_pct != null) {
      lo = e.lower_limit_pct
      hi = e.upper_limit_pct ?? 125
      break
    }
  }
  const coPrimary = (results.co_primary || ['cmax', 'auc0inf']).map(m => METRIC_LABEL[m] || m).join(' & ')

  return (
    <div className="space-y-5">
      {/* Verdict banner */}
      <div role="status" className={cx('rounded-card border px-5 py-4', v.box)}>
        <div className="flex items-start gap-3">
          <span className={cx('mt-0.5 flex flex-none [&>svg]:h-5 [&>svg]:w-5', v.text)}>{v.icon}</span>
          <div>
            <div className={cx('text-h3', v.text)}>Overall bioequivalence: {v.label}</div>
            <div className="mt-0.5 text-small text-fg-muted">
              Co-primary endpoints: {coPrimary} · acceptance range {pct(lo)}–{pct(hi)}%
              {results.design_key === 'replicate' && ' · reference-scaled ABE (RSABE)'}
            </div>
          </div>
        </div>
      </div>

      {/* Pivotal BE table */}
      <Card padding="none" className="overflow-hidden">
        <div className="flex flex-wrap items-center gap-2 border-b border-line px-5 py-3.5">
          <h3 className="text-h3 text-fg">Pivotal bioequivalence assessment</h3>
          <Badge variant="neutral">Locked</Badge>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-body">
            <thead className="bg-surface-muted text-fg-subtle">
              <tr className="eyebrow">
                <th scope="col" className="px-4 py-2.5 text-left font-semibold">Parameter</th>
                <th scope="col" className="px-4 py-2.5 text-right font-semibold">N</th>
                <th scope="col" className="px-4 py-2.5 text-right font-semibold">Point est. (%)</th>
                <th scope="col" className="px-4 py-2.5 text-right font-semibold">90% CI (%)</th>
                <th scope="col" className="px-4 py-2.5 text-right font-semibold">Intra CV (%)</th>
                <th scope="col" className="px-4 py-2.5 text-center font-semibold">Within {pct(lo)}–{pct(hi)}%</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-[color:var(--border)]">
              {order.map(m => {
                const e = be[m]
                return (
                  <tr key={m} className="text-fg-muted">
                    <th scope="row" className="px-4 py-2.5 text-left font-semibold text-fg">{METRIC_LABEL[m] || m}</th>
                    {e.error ? (
                      <td className="px-4 py-2.5 text-small text-[color:var(--warn-fg)]" colSpan={5}>{e.error}</td>
                    ) : (
                      <>
                        <td className="px-4 py-2.5 text-right tabular-nums">{e.n ?? '—'}</td>
                        <td className="px-4 py-2.5 text-right font-semibold tabular-nums text-fg">{pct(e.point_estimate_pct)}</td>
                        <td className="px-4 py-2.5 text-right tabular-nums">{pct(e.ci_lower_pct)} – {pct(e.ci_upper_pct)}</td>
                        <td className="px-4 py-2.5 text-right tabular-nums">{pct(e.cv_intra_pct)}</td>
                        <td className="px-4 py-2.5 text-center">
                          {e.within_be_limits
                            ? <Badge variant="success">Yes</Badge>
                            : <Badge variant="danger">No</Badge>}
                        </td>
                      </>
                    )}
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
        <p className="border-t border-line px-5 py-3 text-small text-fg-subtle">
          Every figure is computed deterministically from your uploaded data (cross-checked in R). Percentages are shown to 2 decimals per ICH convention.
        </p>
      </Card>

      {/* Disposition */}
      <div>
        <h3 className="mb-2 text-h3 text-fg">Subject disposition</h3>
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <Stat label="Subjects" value={disp.n_subjects} />
          <Stat label="Treatments" value={(disp.treatments || []).join(', ')} />
          <Stat label="Periods" value={(disp.periods || []).join(', ')} />
          <Stat label="Sequences" value={(disp.sequences || []).join(', ')} />
        </div>
        {disp.n_blq != null && (
          <p className="mt-2 text-small text-fg-subtle">
            {disp.n_blq} below-limit-of-quantification observation(s){disp.lloq != null ? ` · LLOQ ${disp.lloq}` : ''}.
          </p>
        )}
      </div>
    </div>
  )
}
