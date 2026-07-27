import { useCallback, useEffect, useState } from 'react'
import { api } from '../api/client'
import { SECTION_DISPLAY_NAMES } from '../types'
import type { QCReport, QCStatus, SectionName } from '../types'

interface Props {
  protocolId: string
  onJumpTo: (section: string) => void
}

const STATUS_META: Record<
  QCStatus,
  { label: string; text: string; bg: string; border: string; bar: string; icon: JSX.Element }
> = {
  fail: {
    label: 'Fail', text: 'text-red-700', bg: 'bg-red-50', border: 'border-red-200', bar: 'bg-red-500',
    icon: <path d="M5 5l6 6M11 5l-6 6" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />,
  },
  assumption: {
    label: 'Assumption', text: 'text-violet-700', bg: 'bg-violet-50', border: 'border-violet-200', bar: 'bg-violet-500',
    icon: <path d="M8 2.5a3 3 0 013 3c0 2-2.5 2.2-2.5 4M8 12.5v.01" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />,
  },
  warning: {
    label: 'Warning', text: 'text-amber-700', bg: 'bg-amber-50', border: 'border-amber-200', bar: 'bg-amber-500',
    icon: <path d="M8 2l6 11H2L8 2zM8 6.5v3M8 11.5v.01" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />,
  },
  pass: {
    label: 'Pass', text: 'text-emerald-700', bg: 'bg-emerald-50', border: 'border-emerald-200', bar: 'bg-emerald-500',
    icon: <path d="M3 8.5l3 3 7-7.5" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />,
  },
  not_applicable: {
    label: 'N/A', text: 'text-slate-500', bg: 'bg-slate-50', border: 'border-slate-200', bar: 'bg-slate-300',
    icon: <path d="M3.5 8h9" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />,
  },
}

const ORDER: QCStatus[] = ['fail', 'assumption', 'warning', 'pass', 'not_applicable']

function StatusIcon({ status }: { status: QCStatus }) {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" className={STATUS_META[status].text}>
      {STATUS_META[status].icon}
    </svg>
  )
}

export default function QCPanel({ protocolId, onJumpTo }: Props) {
  const [report, setReport] = useState<QCReport | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [lastChecked, setLastChecked] = useState<Date | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      setReport(await api.getQC(protocolId))
      setLastChecked(new Date())
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to run QC')
    } finally {
      setLoading(false)
    }
  }, [protocolId])

  useEffect(() => { load() }, [load])

  const ready = report?.submission_ready
  const failCount = report?.counts.fail ?? 0

  return (
    <aside className="w-96 flex-shrink-0 border-l border-slate-200 bg-white flex flex-col overflow-hidden">
      {/* Header */}
      <div className="px-4 pt-4 pb-3 border-b border-slate-100 flex-shrink-0">
        <div className="flex items-center justify-between mb-3">
          <div className="flex items-center gap-2">
            <span className="w-7 h-7 rounded-lg bg-cyan-50 border border-cyan-100 flex items-center justify-center text-cyan-600">
              <svg width="15" height="15" viewBox="0 0 16 16" fill="none">
                <path d="M8 1.5l5.5 2.3v3.4c0 3.1-2.1 5.7-5.5 6.8-3.4-1.1-5.5-3.7-5.5-6.8V3.8L8 1.5z" stroke="currentColor" strokeWidth="1.3" strokeLinejoin="round" />
                <path d="M5.5 8l1.8 1.8L11 6" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            </span>
            <h3 className="text-sm font-bold text-slate-900">Protocol QC</h3>
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
              {loading ? 'Checking…' : 'Re-run'}
            </button>
            {lastChecked && !loading && (
              <span key={lastChecked.getTime()} className="qc-flash text-[10px] text-slate-400 leading-none">
                Checked {lastChecked.toLocaleTimeString()}
              </span>
            )}
          </div>
        </div>

        {/* readiness banner */}
        {report && (
          <div
            className={[
              'rounded-xl px-3 py-2.5 border flex items-center gap-2.5',
              ready ? 'bg-emerald-50 border-emerald-200' : 'bg-red-50 border-red-200',
            ].join(' ')}
          >
            <StatusIcon status={ready ? 'pass' : 'fail'} />
            <div className="leading-tight">
              <p className={`text-xs font-bold ${ready ? 'text-emerald-800' : 'text-red-800'}`}>
                {ready ? 'No blocking issues' : `${failCount} issue${failCount === 1 ? '' : 's'} to resolve`}
              </p>
              <p className={`text-[11px] ${ready ? 'text-emerald-600' : 'text-red-600'}`}>
                {ready ? 'Still requires expert sign-off' : 'Fix the failures below before finalizing'}
              </p>
            </div>
          </div>
        )}

        {/* count chips */}
        {report && (
          <div className="flex flex-wrap gap-1.5 mt-2.5">
            {ORDER.filter(s => (report.counts[s] ?? 0) > 0).map(s => (
              <span key={s} className={`inline-flex items-center gap-1 text-[11px] font-semibold px-2 py-0.5 rounded-md border ${STATUS_META[s].bg} ${STATUS_META[s].border} ${STATUS_META[s].text}`}>
                {report.counts[s]} {STATUS_META[s].label}
              </span>
            ))}
          </div>
        )}
      </div>

      {/* Body */}
      <div className="flex-1 overflow-y-auto px-3 py-3 space-y-2">
        {loading && <p className="text-xs text-slate-400 px-1 py-4 text-center">Running checks…</p>}
        {error && <p className="text-xs text-red-600 bg-red-50 border border-red-200 rounded-lg px-3 py-2">{error}</p>}

        {report && [...report.findings].map(f => {
          const meta = STATUS_META[f.status]
          return (
            <div key={f.id} className={`relative rounded-xl border ${meta.border} ${meta.bg} pl-3 pr-3 py-2.5 overflow-hidden`}>
              <span className={`absolute left-0 top-0 bottom-0 w-1 ${meta.bar}`} />
              <div className="flex items-start gap-2">
                <span className="mt-0.5 flex-shrink-0"><StatusIcon status={f.status} /></span>
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2 flex-wrap">
                    <p className={`text-xs font-bold ${meta.text}`}>{f.title}</p>
                    <span className="text-[10px] uppercase tracking-wide text-slate-400 font-semibold">{f.category}</span>
                  </div>
                  <p className="text-[12px] text-slate-600 leading-relaxed mt-1">{f.detail}</p>
                  {f.section && (
                    <button
                      onClick={() => onJumpTo(f.section!)}
                      className="mt-1.5 text-[11px] font-semibold text-cyan-600 hover:text-cyan-700 inline-flex items-center gap-1 cursor-pointer"
                    >
                      Jump to {SECTION_DISPLAY_NAMES[f.section as SectionName] ?? f.section}
                      <svg width="10" height="10" viewBox="0 0 10 10" fill="none"><path d="M3 2l4 3-4 3" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" /></svg>
                    </button>
                  )}
                </div>
              </div>
            </div>
          )
        })}

        {report && (
          <p className="text-[10px] text-slate-400 leading-relaxed px-1 pt-2 pb-4">
            Automated checks are deterministic and do not call AI. They assist — they do not replace — review and sign-off by a qualified clinical pharmacologist.
          </p>
        )}
      </div>
    </aside>
  )
}
