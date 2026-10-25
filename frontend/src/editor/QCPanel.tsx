import { useCallback, useEffect, useState } from 'react'
import { api } from '../api/client'
import { SECTION_DISPLAY_NAMES } from '../types'
import type { QCReport, QCStatus, SectionName } from '../types'
import { AlertTriangle, Badge, Banner, CheckCircle2, CircleHelp, Info, Shield, XCircle } from '../ui'
import type { FindingTone } from './PanelShell'
import { FINDING_TONE, FindingCard, PanelShell } from './PanelShell'

interface Props {
  protocolId: string
  onJumpTo: (section: string) => void
}

const STATUS_META: Record<QCStatus, { label: string; tone: FindingTone; icon: JSX.Element }> = {
  fail: { label: 'Fail', tone: 'danger', icon: <XCircle /> },
  assumption: { label: 'Assumption', tone: 'info', icon: <Info /> },
  warning: { label: 'Warning', tone: 'warning', icon: <AlertTriangle /> },
  pass: { label: 'Pass', tone: 'success', icon: <CheckCircle2 /> },
  not_applicable: { label: 'N/A', tone: 'neutral', icon: <CircleHelp /> },
}

const ORDER: QCStatus[] = ['fail', 'assumption', 'warning', 'pass', 'not_applicable']

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
    <PanelShell
      title="Protocol QC"
      icon={<Shield />}
      loading={loading}
      onRerun={load}
      loadingLabel="Checking…"
      lastChecked={lastChecked}
      summary={
        report ? (
          <div className="mt-3 space-y-2.5">
            <Banner
              tone={ready ? 'success' : 'danger'}
              title={ready ? 'No blocking issues' : `${failCount} issue${failCount === 1 ? '' : 's'} to resolve`}
            >
              {ready ? 'Still requires expert sign-off.' : 'Fix the failures below before finalizing.'}
            </Banner>
            <div className="flex flex-wrap gap-1.5">
              {ORDER.filter(s => (report.counts[s] ?? 0) > 0).map(s => (
                <Badge key={s} variant={FINDING_TONE[STATUS_META[s].tone].badge}>
                  {report.counts[s]} {STATUS_META[s].label}
                </Badge>
              ))}
            </div>
          </div>
        ) : null
      }
    >
      {loading ? <p className="px-1 py-4 text-center text-small text-fg-subtle">Running checks…</p> : null}
      {error ? <Banner tone="danger">{error}</Banner> : null}

      {report
        ? report.findings.map(f => {
            const meta = STATUS_META[f.status]
            const jumpTarget = f.section
            return (
              <FindingCard
                key={f.id}
                tone={meta.tone}
                icon={meta.icon}
                title={f.title}
                caption={f.category}
                jumpLabel={
                  jumpTarget ? `Jump to ${SECTION_DISPLAY_NAMES[jumpTarget as SectionName] ?? jumpTarget}` : undefined
                }
                onJump={jumpTarget ? () => onJumpTo(jumpTarget) : undefined}
              >
                {f.detail}
              </FindingCard>
            )
          })
        : null}

      {report ? (
        <p className="px-1 pb-4 pt-2 text-[12px] leading-relaxed text-fg-subtle">
          Automated checks are deterministic and do not call AI. They assist, but do not replace, review and sign-off by a
          qualified clinical pharmacologist.
        </p>
      ) : null}
    </PanelShell>
  )
}
