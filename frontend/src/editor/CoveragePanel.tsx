import { useCallback, useEffect, useState } from 'react'
import { api } from '../api/client'
import { SECTION_DISPLAY_NAMES } from '../types'
import type { CoverageReport, CovStatus, SectionName } from '../types'

interface Props {
  protocolId: string
  onJumpTo: (section: string) => void
}

const META: Record<CovStatus, { label: string; text: string; bg: string; border: string; bar: string }> = {
  missing:        { label: 'Missing', text: 'text-red-700', bg: 'bg-red-50', border: 'border-red-200', bar: 'bg-red-500' },
  partial:        { label: 'Partial', text: 'text-amber-700', bg: 'bg-amber-50', border: 'border-amber-200', bar: 'bg-amber-500' },
  covered:        { label: 'Covered', text: 'text-emerald-700', bg: 'bg-emerald-50', border: 'border-emerald-200', bar: 'bg-emerald-500' },
  not_applicable: { label: 'N/A', text: 'text-slate-500', bg: 'bg-slate-50', border: 'border-slate-200', bar: 'bg-slate-300' },
}
const ORDER: CovStatus[] = ['missing', 'partial', 'covered', 'not_applicable']

export default function CoveragePanel({ protocolId, onJumpTo }: Props) {
  const [report, setReport] = useState<CoverageReport | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [lastChecked, setLastChecked] = useState<Date | null>(null)

  const load = useCallback(async () => {
    setLoading(true); setError(null)
    try { setReport(await api.getCoverage(protocolId)); setLastChecked(new Date()) }
    catch (e) { setError(e instanceof Error ? e.message : 'Failed to compute coverage') }
    finally { setLoading(false) }
  }, [protocolId])

  useEffect(() => { load() }, [load])

  const pct = report?.coverage_pct ?? 0
  const ring = pct >= 90 ? '#10b981' : pct >= 70 ? '#f59e0b' : '#ef4444'

  return (
    <aside className="w-96 flex-shrink-0 border-l border-slate-200 bg-white flex flex-col overflow-hidden">
      <div className="px-4 pt-4 pb-3 border-b border-slate-100 flex-shrink-0">
        <div className="flex items-center justify-between mb-3">
          <div className="flex items-center gap-2">
            <span className="w-7 h-7 rounded-lg bg-cyan-50 border border-cyan-100 flex items-center justify-center text-cyan-600">
              <svg width="15" height="15" viewBox="0 0 16 16" fill="none">
                <path d="M2.5 3.5h11M2.5 8h11M2.5 12.5h7" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
              </svg>
            </span>
            <h3 className="text-sm font-bold text-slate-900">Guideline Coverage</h3>
          </div>
          <div className="flex flex-col items-end gap-0.5">
            <button onClick={load} disabled={loading}
              className="text-[11px] font-semibold text-cyan-600 hover:text-cyan-700 disabled:opacity-50 flex items-center gap-1 cursor-pointer">
              <svg width="12" height="12" viewBox="0 0 12 12" fill="none" className={loading ? 'animate-spin' : ''}>
                <path d="M10.5 6a4.5 4.5 0 11-1.3-3.2M10.5 1v2.5H8" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
              {loading ? 'Checking…' : 'Re-run'}
            </button>
            {lastChecked && !loading && (
              <span key={lastChecked.getTime()} className="qc-flash text-[10px] text-slate-400 leading-none">
                Checked {lastChecked.toLocaleTimeString()}
              </span>
            )}
          </div>
        </div>

        {report && (
          <div className="flex items-center gap-3 rounded-xl border border-slate-200 bg-slate-50 px-3 py-2.5">
            {/* progress ring */}
            <svg width="48" height="48" viewBox="0 0 48 48" className="flex-shrink-0 -rotate-90">
              <circle cx="24" cy="24" r="20" fill="none" stroke="#e2e8f0" strokeWidth="5" />
              <circle cx="24" cy="24" r="20" fill="none" stroke={ring} strokeWidth="5" strokeLinecap="round"
                strokeDasharray={`${(pct / 100) * 2 * Math.PI * 20} ${2 * Math.PI * 20}`} />
            </svg>
            <div className="leading-tight">
              <p className="text-2xl font-extrabold text-slate-900 tabular-nums">{pct}%</p>
              <p className="text-[11px] text-slate-500">of applicable requirements covered</p>
            </div>
          </div>
        )}

        {report && (
          <div className="flex flex-wrap gap-1.5 mt-2.5">
            {ORDER.filter(s => (report.counts[s] ?? 0) > 0).map(s => (
              <span key={s} className={`inline-flex items-center gap-1 text-[11px] font-semibold px-2 py-0.5 rounded-md border ${META[s].bg} ${META[s].border} ${META[s].text}`}>
                {report.counts[s]} {META[s].label}
              </span>
            ))}
          </div>
        )}
      </div>

      <div className="flex-1 overflow-y-auto px-3 py-3 space-y-2">
        {loading && <p className="text-xs text-slate-400 px-1 py-4 text-center">Computing coverage…</p>}
        {error && <p className="text-xs text-red-600 bg-red-50 border border-red-200 rounded-lg px-3 py-2">{error}</p>}

        {report && report.items.map(it => {
          const meta = META[it.status]
          return (
            <div key={it.id} className={`relative rounded-xl border ${meta.border} ${meta.bg} pl-3 pr-3 py-2.5 overflow-hidden`}>
              <span className={`absolute left-0 top-0 bottom-0 w-1 ${meta.bar}`} />
              <div className="flex items-start justify-between gap-2">
                <p className={`text-xs font-bold ${meta.text}`}>{it.requirement}</p>
                <span className={`flex-shrink-0 text-[10px] font-bold uppercase tracking-wide ${meta.text}`}>{meta.label}</span>
              </div>
              <p className="text-[11px] text-slate-500 mt-0.5">{it.guidance}</p>
              <p className="text-[12px] text-slate-600 leading-relaxed mt-1">{it.detail}</p>
              {it.sections[0] && it.status !== 'not_applicable' && (
                <button onClick={() => onJumpTo(it.sections[0])}
                  className="mt-1.5 text-[11px] font-semibold text-cyan-600 hover:text-cyan-700 inline-flex items-center gap-1 cursor-pointer">
                  Jump to {SECTION_DISPLAY_NAMES[it.sections[0] as SectionName] ?? it.sections[0]}
                  <svg width="10" height="10" viewBox="0 0 10 10" fill="none"><path d="M3 2l4 3-4 3" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" /></svg>
                </button>
              )}
            </div>
          )
        })}

        {report && (
          <p className="text-[10px] text-slate-400 leading-relaxed px-1 pt-2 pb-4">
            Maps each section to the FDA/ICH requirement it satisfies. Deterministic (no AI); a guide to completeness, not a regulatory clearance.
          </p>
        )}
      </div>
    </aside>
  )
}
