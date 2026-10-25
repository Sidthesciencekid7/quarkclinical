import { useCallback, useEffect, useState } from 'react'
import { api } from '../api/client'
import { SECTION_DISPLAY_NAMES } from '../types'
import type { CoverageReport, CovStatus, SectionName } from '../types'
import { AlertTriangle, Badge, Banner, CheckCircle2, CircleHelp, ClipboardList, XCircle } from '../ui'
import type { FindingTone } from './PanelShell'
import { FINDING_TONE, FindingCard, PanelShell } from './PanelShell'

interface Props {
  protocolId: string
  onJumpTo: (section: string) => void
}

const META: Record<CovStatus, { label: string; tone: FindingTone; icon: JSX.Element }> = {
  missing: { label: 'Missing', tone: 'danger', icon: <XCircle /> },
  partial: { label: 'Partial', tone: 'warning', icon: <AlertTriangle /> },
  covered: { label: 'Covered', tone: 'success', icon: <CheckCircle2 /> },
  not_applicable: { label: 'N/A', tone: 'neutral', icon: <CircleHelp /> },
}
const ORDER: CovStatus[] = ['missing', 'partial', 'covered', 'not_applicable']

export default function CoveragePanel({ protocolId, onJumpTo }: Props) {
  const [report, setReport] = useState<CoverageReport | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [lastChecked, setLastChecked] = useState<Date | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      setReport(await api.getCoverage(protocolId))
      setLastChecked(new Date())
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to compute coverage')
    } finally {
      setLoading(false)
    }
  }, [protocolId])

  useEffect(() => { load() }, [load])

  const pct = report?.coverage_pct ?? 0
  const ring = pct >= 90 ? '#10b981' : pct >= 70 ? '#f59e0b' : '#ef4444'

  return (
    <PanelShell
      title="Guideline coverage"
      icon={<ClipboardList />}
      loading={loading}
      onRerun={load}
      loadingLabel="Checking…"
      lastChecked={lastChecked}
      summary={
        report ? (
          <div className="mt-3 space-y-2.5">
            <div className="flex items-center gap-3 rounded-xl border border-line bg-surface-muted px-3 py-2.5">
              <svg width="48" height="48" viewBox="0 0 48 48" className="flex-none -rotate-90" aria-hidden>
                <circle cx="24" cy="24" r="20" fill="none" stroke="#e4e4e7" strokeWidth="5" />
                <circle
                  cx="24" cy="24" r="20" fill="none" stroke={ring} strokeWidth="5" strokeLinecap="round"
                  strokeDasharray={`${(pct / 100) * 2 * Math.PI * 20} ${2 * Math.PI * 20}`}
                />
              </svg>
              <div className="leading-tight">
                <p className="text-h2 tabular-nums text-fg">{pct}%</p>
                <p className="text-small text-fg-muted">of applicable requirements covered</p>
              </div>
            </div>
            <div className="flex flex-wrap gap-1.5">
              {ORDER.filter(s => (report.counts[s] ?? 0) > 0).map(s => (
                <Badge key={s} variant={FINDING_TONE[META[s].tone].badge}>
                  {report.counts[s]} {META[s].label}
                </Badge>
              ))}
            </div>
          </div>
        ) : null
      }
    >
      {loading ? <p className="px-1 py-4 text-center text-small text-fg-subtle">Computing coverage…</p> : null}
      {error ? <Banner tone="danger">{error}</Banner> : null}

      {report
        ? report.items.map(it => {
            const meta = META[it.status]
            const target = it.sections[0]
            const canJump = !!target && it.status !== 'not_applicable'
            return (
              <FindingCard
                key={it.id}
                tone={meta.tone}
                icon={meta.icon}
                title={it.requirement}
                trailing={meta.label}
                hint={it.guidance}
                jumpLabel={canJump ? `Jump to ${SECTION_DISPLAY_NAMES[target as SectionName] ?? target}` : undefined}
                onJump={canJump ? () => onJumpTo(target) : undefined}
              >
                {it.detail}
              </FindingCard>
            )
          })
        : null}

      {report ? (
        <p className="px-1 pb-4 pt-2 text-[12px] leading-relaxed text-fg-subtle">
          Maps each section to the FDA/ICH requirement it satisfies. Deterministic (no AI); a guide to completeness, not a
          regulatory clearance.
        </p>
      ) : null}
    </PanelShell>
  )
}
