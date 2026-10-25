import { useCallback, useEffect, useState } from 'react'
import { api } from '../api/client'
import type { RiskLevel, RiskScore } from '../types'
import { AlertTriangle, Badge, Banner, CheckCircle2, Info } from '../ui'
import type { FindingTone } from './PanelShell'
import { FINDING_TONE, FindingCard, PanelShell } from './PanelShell'

interface Props {
  protocolId: string
  onJumpTo?: (name: string) => void
}

const LEVEL_META: Record<RiskLevel, { label: string; tone: FindingTone; icon: JSX.Element }> = {
  high: { label: 'High risk', tone: 'danger', icon: <AlertTriangle /> },
  moderate: { label: 'Moderate risk', tone: 'warning', icon: <Info /> },
  low: { label: 'Low risk', tone: 'success', icon: <CheckCircle2 /> },
}

const ORDER: RiskLevel[] = ['high', 'moderate', 'low']

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
  const tone = FINDING_TONE[meta.tone]
  const counts = ORDER.map(l => ({
    level: l,
    n: report?.factors.filter(f => f.severity === l).length ?? 0,
  })).filter(c => c.n > 0)

  return (
    <PanelShell
      title="Feasibility risk"
      icon={<AlertTriangle />}
      loading={loading}
      onRerun={load}
      rerunLabel="Re-score"
      loadingLabel="Scoring…"
      lastChecked={lastChecked}
      lastCheckedPrefix="Scored"
      summary={
        report ? (
          <div className="mt-3 space-y-2.5">
            <div className={`rounded-xl border px-3 py-2.5 ${tone.box}`}>
              <div className="flex items-baseline gap-2">
                <p className={`text-small font-semibold ${tone.text}`}>{meta.label}</p>
                <span className={`text-[11px] font-semibold uppercase tracking-wide tabular-nums ${tone.text}`}>
                  {report.overall_score}/100
                </span>
              </div>
              <p className="text-[12px] text-fg-muted">
                {report.factors.length} factor{report.factors.length === 1 ? '' : 's'} considered. Higher score means riskier.
              </p>
              <div
                role="meter"
                aria-label="Overall risk score"
                aria-valuemin={0}
                aria-valuemax={100}
                aria-valuenow={Math.max(0, Math.min(100, report.overall_score))}
                className="mt-2 h-1.5 w-full overflow-hidden rounded-full border border-line bg-white"
              >
                <div
                  className={`h-full transition-all ${tone.bar}`}
                  style={{ width: `${Math.max(0, Math.min(100, report.overall_score))}%` }}
                />
              </div>
            </div>
            {counts.length > 0 ? (
              <div className="flex flex-wrap gap-1.5">
                {counts.map(c => (
                  <Badge key={c.level} variant={FINDING_TONE[LEVEL_META[c.level].tone].badge}>
                    {c.n} {LEVEL_META[c.level].label}
                  </Badge>
                ))}
              </div>
            ) : null}
          </div>
        ) : null
      }
    >
      {loading ? <p className="px-1 py-4 text-center text-small text-fg-subtle">Scoring feasibility…</p> : null}
      {error ? <Banner tone="danger">{error}</Banner> : null}
      {report && report.factors.length === 0 && !loading ? (
        <p className="px-1 py-4 text-center text-small text-fg-subtle">No risk factors identified.</p>
      ) : null}

      {report
        ? ORDER.flatMap(lvl =>
            report.factors
              .map((f, i) => ({ f, i }))
              .filter(({ f }) => f.severity === lvl)
              .map(({ f, i }) => {
                const fm = LEVEL_META[f.severity] ?? LEVEL_META.low
                return (
                  <FindingCard key={`${lvl}-${i}`} tone={fm.tone} icon={fm.icon} title={f.label} caption={f.severity}>
                    {f.detail}
                  </FindingCard>
                )
              }),
          )
        : null}

      {report ? (
        <p className="px-1 pb-4 pt-2 text-[12px] leading-relaxed text-fg-subtle">
          Risk scoring is deterministic and does not call AI. It weights the computed design, variability inputs, guideline
          currency and QC results. It assists, but does not replace, operational and clinical judgement.
        </p>
      ) : null}
    </PanelShell>
  )
}
