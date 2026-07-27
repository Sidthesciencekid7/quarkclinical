import type { CsrResults } from '../types'

/** Read-only display of the LOCKED, deterministically-computed CSR results:
 *  the bioequivalence verdict, the pivotal per-metric table, and subject
 *  disposition. These numbers are never editable — they are the analysis. */

const METRIC_LABEL: Record<string, string> = {
  cmax: 'Cmax',
  auc0t: 'AUC0-t',
  auc0inf: 'AUC0-∞',
}
const METRIC_ORDER = ['cmax', 'auc0t', 'auc0inf']

function pct(v?: number | null): string {
  return v === undefined || v === null || Number.isNaN(v) ? '—' : v.toFixed(2)
}

const VERDICT: Record<string, { label: string; cls: string; badge: string }> = {
  bioequivalent: {
    label: 'Bioequivalent',
    cls: 'bg-emerald-50 border-emerald-200 text-emerald-800',
    badge: 'bg-emerald-600',
  },
  not_bioequivalent: {
    label: 'Not bioequivalent',
    cls: 'bg-red-50 border-red-200 text-red-800',
    badge: 'bg-red-600',
  },
  indeterminate: {
    label: 'Indeterminate',
    cls: 'bg-amber-50 border-amber-200 text-amber-800',
    badge: 'bg-amber-500',
  },
}

function Stat({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="rounded-xl border border-slate-200 bg-white px-4 py-3">
      <div className="text-[11px] font-semibold uppercase tracking-wide text-slate-400">{label}</div>
      <div className="text-sm font-semibold text-slate-800 mt-0.5 break-words">{value ?? '—'}</div>
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
      <div className={`rounded-2xl border px-5 py-4 ${v.cls}`}>
        <div className="flex items-start gap-3">
          <span className={`mt-0.5 inline-flex h-6 w-6 flex-shrink-0 items-center justify-center rounded-full text-white text-sm font-bold ${v.badge}`}>
            {results.verdict === 'bioequivalent' ? '✓' : results.verdict === 'not_bioequivalent' ? '✕' : '!'}
          </span>
          <div>
            <div className="font-bold">Overall bioequivalence: {v.label}</div>
            <div className="text-xs opacity-80 mt-0.5">
              Co-primary endpoints: {coPrimary} · acceptance range {pct(lo)}–{pct(hi)}%
              {results.design_key === 'replicate' && ' · reference-scaled ABE (RSABE)'}
            </div>
          </div>
        </div>
      </div>

      {/* Pivotal BE table */}
      <div>
        <h3 className="text-sm font-bold text-slate-800 mb-2">Pivotal Bioequivalence Assessment</h3>
        <div className="overflow-x-auto rounded-xl border border-slate-200">
          <table className="w-full text-sm">
            <thead className="bg-slate-50 text-slate-500 text-[11px] uppercase tracking-wide">
              <tr>
                <th className="px-3 py-2.5 text-left font-semibold">Parameter</th>
                <th className="px-3 py-2.5 text-right font-semibold">N</th>
                <th className="px-3 py-2.5 text-right font-semibold">Point Est. (%)</th>
                <th className="px-3 py-2.5 text-right font-semibold">90% CI (%)</th>
                <th className="px-3 py-2.5 text-right font-semibold">Intra CV (%)</th>
                <th className="px-3 py-2.5 text-center font-semibold">Within {pct(lo)}–{pct(hi)}%</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {order.map(m => {
                const e = be[m]
                return (
                  <tr key={m} className="text-slate-700">
                    <td className="px-3 py-2.5 font-semibold text-slate-800">{METRIC_LABEL[m] || m}</td>
                    {e.error ? (
                      <td className="px-3 py-2.5 text-amber-700 text-xs" colSpan={5}>{e.error}</td>
                    ) : (
                      <>
                        <td className="px-3 py-2.5 text-right tabular-nums">{e.n ?? '—'}</td>
                        <td className="px-3 py-2.5 text-right tabular-nums font-semibold text-slate-900">{pct(e.point_estimate_pct)}</td>
                        <td className="px-3 py-2.5 text-right tabular-nums">{pct(e.ci_lower_pct)} – {pct(e.ci_upper_pct)}</td>
                        <td className="px-3 py-2.5 text-right tabular-nums">{pct(e.cv_intra_pct)}</td>
                        <td className="px-3 py-2.5 text-center">
                          {e.within_be_limits
                            ? <span className="text-emerald-700 font-bold">Yes</span>
                            : <span className="text-red-600 font-bold">No</span>}
                        </td>
                      </>
                    )}
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
        <p className="text-[11px] text-slate-400 mt-2">
          Every figure is computed deterministically from your uploaded data (cross-checked in R). Percentages are shown to 2 decimals per ICH convention.
        </p>
      </div>

      {/* Disposition */}
      <div>
        <h3 className="text-sm font-bold text-slate-800 mb-2">Subject Disposition</h3>
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
          <Stat label="Subjects" value={disp.n_subjects} />
          <Stat label="Treatments" value={(disp.treatments || []).join(', ')} />
          <Stat label="Periods" value={(disp.periods || []).join(', ')} />
          <Stat label="Sequences" value={(disp.sequences || []).join(', ')} />
        </div>
        {disp.n_blq != null && (
          <p className="text-[11px] text-slate-400 mt-2">
            {disp.n_blq} below-limit-of-quantification observation(s){disp.lloq != null ? ` · LLOQ ${disp.lloq}` : ''}.
          </p>
        )}
      </div>
    </div>
  )
}
