import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { Badge, Banner, Button, ButtonLink, ClipboardList, EmptyState, FileText, RefreshCw } from '../../ui'
import type { BadgeVariant } from '../../ui'
import { listAllStudyReports } from '../../api/client'
import type { StudyReportListItem } from '../../api/client'
import { useActingUser } from '../../lib/actingUser'
import { capitalize, relativeTime, STUDY_TYPE_LABELS } from '../../shell/format'

const INPUT_KIND_LABEL: Record<string, string> = {
  params: 'PK parameters',
  nca: 'Concentration-time data',
}

const VERDICT_LABEL: Record<string, { label: string; variant: BadgeVariant }> = {
  bioequivalent: { label: 'Bioequivalent', variant: 'success' },
  not_bioequivalent: { label: 'Not bioequivalent', variant: 'danger' },
  indeterminate: { label: 'Indeterminate', variant: 'warning' },
}

function statusOf(r: StudyReportListItem): { label: string; variant: BadgeVariant } {
  if (r.generation_status === 'generating') return { label: 'Writing report', variant: 'info' }
  if (r.generation_status === 'failed') return { label: 'Failed', variant: 'danger' }
  if (r.generation_status === 'complete') return { label: 'Report ready', variant: 'success' }
  if (r.has_results === false) return { label: 'Awaiting results', variant: 'neutral' }
  return { label: 'In progress', variant: 'neutral' }
}

/** `/app/reports`: every clinical study report in the org, newest first. */
export default function StudyReportsPage() {
  const { currentUser, loading: identityLoading } = useActingUser()
  const [rows, setRows] = useState<StudyReportListItem[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(() => {
    setRows(null)
    setError(null)
    let cancelled = false
    listAllStudyReports()
      .then(list => {
        if (!cancelled) setRows(Array.isArray(list) ? list : [])
      })
      .catch(e => {
        if (!cancelled) setError(e instanceof Error ? e.message : 'Could not load study reports.')
      })
    return () => {
      cancelled = true
    }
  }, [])

  useEffect(() => {
    if (identityLoading || !currentUser) return
    return load()
  }, [identityLoading, currentUser?.id, load])

  return (
    <div className="mx-auto w-full max-w-4xl px-4 py-8 sm:px-8 sm:py-10">
      <div className="mb-6 flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-h1 text-fg">Study reports</h1>
          <p className="mt-1 text-body text-fg-muted">
            Clinical study reports your organization has started. Open a protocol to begin a new one.
          </p>
        </div>
        <ButtonLink as={Link} to="/app/protocols" variant="secondary" leftIcon={<FileText />}>
          Go to protocols
        </ButtonLink>
      </div>

      {rows === null && !error ? (
        <div className="space-y-3" aria-busy="true" aria-label="Loading study reports">
          {[0, 1, 2].map(i => (
            <div key={i} className="h-[72px] animate-pulse rounded-card border border-line bg-surface-muted" />
          ))}
        </div>
      ) : null}

      {error ? (
        <Banner
          tone="danger"
          title="Could not load study reports"
          action={
            <Button size="sm" variant="secondary" leftIcon={<RefreshCw />} onClick={load}>
              Retry
            </Button>
          }
        >
          {error}
        </Banner>
      ) : null}

      {rows && rows.length === 0 ? (
        <div className="rounded-card border border-line bg-surface">
          <EmptyState
            icon={<ClipboardList />}
            title="No study reports yet"
            description="Once a study has results, open its protocol and choose Study report to analyse the data and draft the CSR."
            action={
              <ButtonLink as={Link} to="/app/protocols" variant="primary">
                Open protocols
              </ButtonLink>
            }
          />
        </div>
      ) : null}

      {rows && rows.length > 0 ? (
        <ul className="space-y-2.5">
          {rows.map(r => {
            const status = statusOf(r)
            const verdict = r.verdict ? VERDICT_LABEL[r.verdict] : null
            const studyType = (r as StudyReportListItem & { study_type?: string }).study_type
            return (
              <li key={r.id}>
                <Link
                  to={`/app/protocols/${r.protocol_id}/report`}
                  className="card flex items-center gap-4 p-4 text-fg no-underline transition duration-fast hover:border-line-strong hover:shadow-pop"
                >
                  <span
                    aria-hidden
                    className="flex h-11 w-11 flex-none items-center justify-center rounded-xl bg-surface-muted text-fg-muted [&>svg]:h-5 [&>svg]:w-5"
                  >
                    <ClipboardList />
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-body font-medium">
                      {capitalize(r.drug_inn ?? 'Study')}
                      {studyType ? ` · ${STUDY_TYPE_LABELS[studyType] ?? studyType}` : ''}
                    </span>
                    <span className="block truncate text-small text-fg-muted">
                      {r.protocol_title ? `${r.protocol_title} · ` : ''}
                      {r.input_kind ? `${INPUT_KIND_LABEL[r.input_kind] ?? r.input_kind} · ` : ''}
                      {r.version ? `v${r.version} · ` : ''}
                      Updated {relativeTime(r.updated_at)}
                    </span>
                  </span>
                  <span className="flex flex-none flex-wrap items-center justify-end gap-1.5">
                    {verdict ? <Badge variant={verdict.variant}>{verdict.label}</Badge> : null}
                    <Badge variant={status.variant}>{status.label}</Badge>
                  </span>
                </Link>
              </li>
            )
          })}
        </ul>
      ) : null}
    </div>
  )
}
