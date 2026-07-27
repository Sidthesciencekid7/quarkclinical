import { useCallback, useEffect, useState } from 'react'
import { api } from '../api/client'
import type { RiskLevel, RiskScore } from '../types'

interface Props {
  protocolId: string
  onJumpTo?: (name: string) => void
}

const LEVEL_META: Record<
  RiskLevel,
  { label: string; text: string; bg: string; border: string; bar: string; icon: JSX.Element }
> = {
  high: {
    label: 'High risk', text: 'text-red-700', bg: 'bg-red-50', border: 'border-red-200', bar: 'bg-red-500',
    icon: <path d="M8 2l6 11H2L8 2zM8 6.5v3M8 11.5v.01" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />,
  },
  moderate: {
    label: 'Moderate risk', text: 'text-amber-700', bg: 'bg-amber-50', border: 'border-amber-200', bar: 'bg-amber-500',
    icon: <path d="M8 2.5v6M8 12.5v.01M8 1.5a6.5 6.5 0 100 13 6.5 6.5 0 000-13z" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />,
  },
  low: {
    label: 'Low risk', text: 'text-emerald-700', bg: 'bg-emerald-50', border: 'border-emerald-200', bar: 'bg-emerald-500',
    icon: <path d="M3 8.5l3 3 7-7.5" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />,
  },
}

const ORDER: RiskLevel[] = ['high', 'moderate', 'low']

function LevelIcon({ level }: { level: RiskLevel }) {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" className={LEVEL_META[level].text}>
      {LEVEL_META[level].icon}
    </svg>
  )
}

export default function RiskPanel({ protocolId }: Props) {
  const [report, setReport] = useState<RiskScore | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [lastChecked, setLastChecked] = useState<Date | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      setReport(await api.getProtocolRisk(protocolId))
      setLastChecked(new Date())
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to score risk')
    } finally {
      setLoading(false)
    }
  }, [protocolId])

  useEffect(() => { load() }, [load])

  const level: RiskLevel = report?.risk_level ?? 'low'
  const meta = LEVEL_META[level]
  const counts = ORDER.map(l => ({
    level: l,
    n: report?.factors.filter(f => f.severity === l).length ?? 0,
  })).filter(c => c.n > 0)

  return (
    <aside className="w-96 flex-shrink-0 border-l border-slate-200 bg-white flex flex-col overflow-hidden">
      {/* Header */}
      <div className="px-4 pt-4 pb-3 border-b border-slate-100 flex-shrink-0">
        <div className="flex items-center justify-between mb-3">
          <div className="flex items-center gap-2">
            <span className="w-7 h-7 rounded-lg bg-cyan-50 border border-cyan-100 flex items-center justify-center text-cyan-600">
              <svg width="15" height="15" viewBox="0 0 16 16" fill="none">
                <path d="M8 1.8l6.2 11.4H1.8L8 1.8z" stroke="currentColor" strokeWidth="1.3" strokeLinejoin="round" />
                <path d="M8 6.4v3M8 11.4v.01" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
              </svg>
            </span>
            <h3 className="text-sm font-bold text-slate-900">Feasibility Risk</h3>
          </div>
          <div className="flex flex-col items-end gap-0.5">
            <button
              onClick={load}
              disabled={loading}
              className="text-[11px] font-semibold text-cyan-600 hover:text-cyan-700 disabled:opacity-50 flex items-center gap-1 cursor-pointer"
            >
              <svg width="12" height="12" viewBox="0 0 12 12" fill="none" className={loading ? 'animate-spin' : ''}>
                <path d="M10.5 6a4.5 4.5 0 11-1.3-3.2M10.5 1v2.5H8" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
              {loading ? 'Scoring…' : 'Re-score'}
            </button>
            {lastChecked && !loading && (
              <span key={lastChecked.getTime()} className="qc-flash text-[10px] text-slate-400 leading-none">
                Scored {lastChecked.toLocaleTimeString()}
              </span>
            )}
          </div>
        </div>

        {/* score banner */}
        {report && (
          <div className={`rounded-xl px-3 py-2.5 border flex items-center gap-3 ${meta.bg} ${meta.border}`}>
            <LevelIcon level={level} />
            <div className="leading-tight flex-1 min-w-0">
              <div className="flex items-baseline gap-1.5">
                <p className={`text-xs font-bold ${meta.text}`}>{meta.label}</p>
                <span className={`text-[10px] font-semibold uppercase tracking-wide ${meta.text} opacity-70`}>
                  {report.overall_score}/100
                </span>
              </div>
              <p className={`text-[11px] ${meta.text} opacity-80`}>
                {report.factors.length} factor{report.factors.length === 1 ? '' : 's'} considered · higher score = riskier
              </p>
              {/* score bar */}
              <div className="mt-1.5 h-1.5 w-full rounded-full bg-white/70 overflow-hidden border border-black/5">
                <div
                  className={`h-full ${meta.bar} transition-all`}
                  style={{ width: `${Math.max(0, Math.min(100, report.overall_score))}%` }}
                />
              </div>
            </div>
          </div>
        )}

        {/* count chips */}
        {report && counts.length > 0 && (
          <div className="flex flex-wrap gap-1.5 mt-2.5">
            {counts.map(c => (
              <span
                key={c.level}
                className={`inline-flex items-center gap-1 text-[11px] font-semibold px-2 py-0.5 rounded-md border ${LEVEL_META[c.level].bg} ${LEVEL_META[c.level].border} ${LEVEL_META[c.level].text}`}
              >
                {c.n} {LEVEL_META[c.level].label}
              </span>
            ))}
          </div>
        )}
      </div>

      {/* Body */}
      <div className="flex-1 overflow-y-auto px-3 py-3 space-y-2">
        {loading && <p className="text-xs text-slate-400 px-1 py-4 text-center">Scoring feasibility…</p>}
        {error && <p className="text-xs text-red-600 bg-red-50 border border-red-200 rounded-lg px-3 py-2">{error}</p>}
        {report && report.factors.length === 0 && !loading && (
          <p className="text-xs text-slate-400 px-1 py-4 text-center">No risk factors identified.</p>
        )}

        {report && ORDER.flatMap(lvl =>
          report.factors
            .map((f, i) => ({ f, i }))
            .filter(({ f }) => f.severity === lvl)
            .map(({ f, i }) => {
              const fm = LEVEL_META[f.severity] ?? LEVEL_META.low
              return (
                <div key={`${lvl}-${i}`} className={`relative rounded-xl border ${fm.border} ${fm.bg} pl-3 pr-3 py-2.5 overflow-hidden`}>
                  <span className={`absolute left-0 top-0 bottom-0 w-1 ${fm.bar}`} />
                  <div className="flex items-start gap-2">
                    <span className="mt-0.5 flex-shrink-0"><LevelIcon level={f.severity} /></span>
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2 flex-wrap">
                        <p className={`text-xs font-bold ${fm.text}`}>{f.label}</p>
                        <span className="text-[10px] uppercase tracking-wide text-slate-400 font-semibold">{f.severity}</span>
                      </div>
                      <p className="text-[12px] text-slate-600 leading-relaxed mt-1">{f.detail}</p>
                    </div>
                  </div>
                </div>
              )
            }),
        )}

        {report && (
          <p className="text-[10px] text-slate-400 leading-relaxed px-1 pt-2 pb-4">
            Risk scoring is deterministic and does not call AI. It weights the computed design,
            variability inputs, guideline currency and QC results — it assists, but does not replace,
            operational and clinical judgement.
          </p>
        )}
      </div>
    </aside>
  )
}
