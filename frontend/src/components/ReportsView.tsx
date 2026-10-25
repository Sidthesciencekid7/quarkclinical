import { useEffect, useMemo, useState } from 'react'
import type { MouseEvent } from 'react'
import { api } from '../api/client'
import { useActingUser } from '../lib/actingUser'
import { capitalize, relativeTime, STUDY_TYPE_LABELS } from '../shell/format'
import type { ProtocolSummary } from '../types'
import {
  ArrowRight,
  Badge,
  Banner,
  Button,
  Card,
  ClipboardList,
  Copy,
  EmptyState,
  FileText,
  IconButton,
  Input,
  Plus,
  Search,
  TabPanel,
  Tabs,
  X,
} from '../ui'
import type { BadgeVariant } from '../ui'

interface Props {
  /** Open a past protocol in the editor (parent fetches full sections). */
  onOpen: (summary: ProtocolSummary) => void
  /** Start a new protocol (switch to the wizard). */
  onNew: () => void
  /** Open the Clinical Study Report (post-study analysis) flow for this protocol. */
  onOpenCsr: (summary: ProtocolSummary) => void
  /** The id currently being opened, so its card can show a spinner. */
  openingId: string | null
  /** Error message from the last failed attempt to open a report, if any. */
  openError: string | null
  /** Dismiss the current open-report error banner. */
  onDismissOpenError: () => void
}

type StatusFilter = 'all' | 'draft' | 'review' | 'approved'

const STATUS_TABS: { id: StatusFilter; label: string }[] = [
  { id: 'all', label: 'All' },
  { id: 'draft', label: 'Draft' },
  { id: 'review', label: 'In review' },
  { id: 'approved', label: 'Approved' },
]

const STATUS_BADGE: Record<string, { label: string; variant: BadgeVariant }> = {
  draft: { label: 'Draft', variant: 'neutral' },
  review: { label: 'In review', variant: 'warning' },
  approved: { label: 'Approved', variant: 'success' },
}

const TABS_ID = 'protocol-status-tabs'

/** Statuses the server may add later fall under "Draft" so a row never vanishes from All. */
function statusKey(r: ProtocolSummary): Exclude<StatusFilter, 'all'> {
  return r.status === 'review' || r.status === 'approved' ? r.status : 'draft'
}

function matchesQuery(r: ProtocolSummary, q: string): boolean {
  if (!q) return true
  const hay = [
    r.drug_inn,
    ...(r.drug_brand_names ?? []),
    STUDY_TYPE_LABELS[r.study_type] ?? r.study_type,
    r.guideline_version,
  ]
    .join(' ')
    .toLowerCase()
  return q
    .toLowerCase()
    .split(/\s+/)
    .filter(Boolean)
    .every(term => hay.includes(term))
}

export default function ReportsView({ onOpen, onNew, onOpenCsr, openingId, openError, onDismissOpenError }: Props) {
  const [reports, setReports] = useState<ProtocolSummary[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  // Id of the protocol currently being duplicated. Doubles as the in-flight
  // lock: while it's non-null EVERY Duplicate button is disabled, so a
  // double-click (or a click on a second row) can't create two clones.
  const [cloningId, setCloningId] = useState<string | null>(null)
  const [cloneError, setCloneError] = useState<string | null>(null)
  const [tab, setTab] = useState<StatusFilter>('all')
  const [query, setQuery] = useState('')

  // The reports list is org-scoped to the acting user, so we must wait until
  // identity is resolved before fetching, otherwise the request goes out with
  // no X-User-Id (and 401s) on a fresh browser where the picker hasn't set the
  // acting user yet. Re-fetch when the acting user changes.
  const { currentUser, loading: identityLoading, error: identityError } = useActingUser()

  useEffect(() => {
    if (identityLoading) return
    let cancelled = false
    setReports(null)
    setError(null)
    if (!currentUser) {
      setError(identityError ?? 'No user available, cannot load protocols.')
      return
    }
    api.listProtocols()
      .then(list => { if (!cancelled) setReports(list) })
      .catch(e => { if (!cancelled) setError(e instanceof Error ? e.message : 'Failed to load protocols') })
    return () => { cancelled = true }
  }, [identityLoading, currentUser?.id, identityError])

  /** Duplicate a past protocol as a fresh draft, then refresh the list so the
   *  copy shows up at the top (the list is newest-first). No AI re-run happens
   *  server-side, so this returns immediately. */
  async function handleDuplicate(r: ProtocolSummary) {
    if (cloningId) return  // guard: an earlier click is still in flight
    setCloningId(r.id)
    setCloneError(null)
    try {
      await api.cloneProtocol(r.id)
      setReports(await api.listProtocols())
    } catch (e) {
      setCloneError(e instanceof Error ? e.message : 'Failed to duplicate protocol')
    } finally {
      setCloningId(null)
    }
  }

  const counts = useMemo(() => {
    const c: Record<StatusFilter, number> = { all: 0, draft: 0, review: 0, approved: 0 }
    for (const r of reports ?? []) {
      c.all += 1
      c[statusKey(r)] += 1
    }
    return c
  }, [reports])

  const visible = useMemo(
    () => (reports ?? []).filter(r => (tab === 'all' || statusKey(r) === tab) && matchesQuery(r, query.trim())),
    [reports, tab, query],
  )

  const tabItems = STATUS_TABS.map(t => ({
    id: t.id,
    label: t.label,
    badge: reports ? (
      <span className="ml-0.5 text-small tabular-nums text-fg-subtle">{counts[t.id]}</span>
    ) : undefined,
  }))

  const filtering = tab !== 'all' || query.trim() !== ''

  return (
    <div className="mx-auto w-full max-w-4xl px-4 py-8 sm:px-8 sm:py-10">
      {/* Header row */}
      <div className="mb-6 flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-h1 text-fg">Protocols</h1>
          <p className="mt-1 text-body text-fg-muted">
            Every protocol your organization has generated. Open one to keep editing, export, or review.
          </p>
        </div>
        <Button onClick={onNew} leftIcon={<Plus aria-hidden />}>
          New study
        </Button>
      </div>

      {/* Opening a report failed: non-fatal, the list itself is still usable */}
      {openError ? (
        <Banner tone="danger" title="Couldn't open that protocol" onDismiss={onDismissOpenError} className="mb-3">
          {openError}
        </Banner>
      ) : null}

      {/* Duplicate failed: non-fatal, the list itself is still usable */}
      {cloneError ? (
        <Banner tone="danger" title="Couldn't duplicate that protocol" onDismiss={() => setCloneError(null)} className="mb-3">
          {cloneError}
        </Banner>
      ) : null}

      {error ? (
        <Banner tone="danger" title="Couldn't load your protocols">
          {error}
        </Banner>
      ) : null}

      {/* Loading */}
      {reports === null && !error ? (
        <div className="space-y-3" role="status" aria-busy="true" aria-label="Loading protocols">
          {[0, 1, 2].map(i => (
            <div key={i} className="h-[76px] animate-pulse rounded-card border border-line bg-surface-muted" />
          ))}
        </div>
      ) : null}

      {/* Nothing generated yet */}
      {reports?.length === 0 ? (
        <Card padding="none">
          <EmptyState
            icon={<FileText />}
            title="No protocols yet"
            description="Describe your first bioequivalence or biosimilar PK study. The protocol will show up here so you can reopen it anytime."
            action={
              <Button onClick={onNew} leftIcon={<Plus aria-hidden />}>
                New study
              </Button>
            }
          />
        </Card>
      ) : null}

      {reports && reports.length > 0 ? (
        <>
          {/* Filters: status tabs + search */}
          <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
            <Tabs
              id={TABS_ID}
              variant="pill"
              ariaLabel="Filter protocols by status"
              items={tabItems}
              value={tab}
              onChange={id => setTab(id as StatusFilter)}
            />
            <div className="w-full sm:w-72">
              <Input
                type="text"
                role="searchbox"
                autoComplete="off"
                shape="pill"
                aria-label="Search protocols by drug or study type"
                placeholder="Search by drug or study type"
                leftIcon={<Search aria-hidden />}
                value={query}
                onChange={e => setQuery(e.target.value)}
                rightSlot={
                  query ? (
                    <IconButton label="Clear search" icon={<X />} size="sm" onClick={() => setQuery('')} />
                  ) : undefined
                }
              />
            </div>
          </div>

          <TabPanel tabsId={TABS_ID} id={tab} value={tab}>
            <p className="sr-only" role="status" aria-live="polite">
              {visible.length} {visible.length === 1 ? 'protocol' : 'protocols'} shown
            </p>

            {visible.length === 0 ? (
              <Card padding="none">
                <EmptyState
                  icon={<Search />}
                  title="No protocols match"
                  description="Try a different status or search term."
                  action={
                    filtering ? (
                      <Button
                        variant="secondary"
                        onClick={() => {
                          setTab('all')
                          setQuery('')
                        }}
                      >
                        Clear filters
                      </Button>
                    ) : undefined
                  }
                />
              </Card>
            ) : (
              <ul className="space-y-2.5">
                {visible.map(r => {
                  const isOpening = openingId === r.id
                  const isCloning = cloningId === r.id
                  const status = STATUS_BADGE[r.status] ?? STATUS_BADGE.draft
                  // Mouse convenience: a click on the card body opens the protocol.
                  // Keyboard and screen-reader users use the explicit Open button.
                  const openFromCard = (e: MouseEvent<HTMLElement>) => {
                    if ((e.target as HTMLElement).closest('button, a')) return
                    if (!isOpening && !cloningId) onOpen(r)
                  }
                  return (
                    <Card
                      as="li"
                      key={r.id}
                      padding="none"
                      onClick={openFromCard}
                      className="flex cursor-pointer flex-wrap items-center gap-x-4 gap-y-3 p-4 transition duration-fast hover:border-line-strong hover:shadow-pop"
                    >
                      <span
                        aria-hidden
                        className="flex h-11 w-11 flex-none items-center justify-center rounded-xl bg-surface-muted text-fg-muted [&>svg]:h-5 [&>svg]:w-5"
                      >
                        <FileText />
                      </span>

                      <div className="min-w-0 flex-1 basis-56">
                        <div className="flex flex-wrap items-center gap-2">
                          <h2 className="truncate text-body font-semibold text-fg">{capitalize(r.drug_inn)}</h2>
                          <Badge variant="info">{STUDY_TYPE_LABELS[r.study_type] ?? r.study_type}</Badge>
                          <Badge variant={status.variant} dot>
                            {status.label}
                          </Badge>
                          {r.guideline_outdated ? (
                            <Badge variant="warning" title="Locked to a superseded guideline">
                              Guideline outdated
                            </Badge>
                          ) : null}
                        </div>
                        <p className="mt-1 truncate text-small text-fg-muted">
                          {r.guideline_version} · v{r.version} · {r.section_count} sections · {relativeTime(r.created_at)}
                        </p>
                      </div>

                      <div className="flex flex-none flex-wrap items-center gap-2">
                        <Button
                          variant="secondary"
                          size="sm"
                          onClick={() => onOpen(r)}
                          loading={isOpening}
                          disabled={isCloning}
                          rightIcon={<ArrowRight aria-hidden />}
                          aria-label={`Open ${capitalize(r.drug_inn)} protocol`}
                        >
                          Open
                        </Button>
                        {/* Start-from-template: server-side copy, no AI re-run. */}
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => handleDuplicate(r)}
                          loading={isCloning}
                          disabled={cloningId !== null || isOpening}
                          leftIcon={<Copy aria-hidden />}
                          title="Duplicate as a new draft: reuses this study design, nothing is regenerated"
                          aria-label={`Duplicate ${capitalize(r.drug_inn)} protocol`}
                        >
                          {isCloning ? 'Duplicating…' : 'Duplicate'}
                        </Button>
                        {/* Post-study: analyze real results, then draft the Clinical Study Report. */}
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => onOpenCsr(r)}
                          disabled={cloningId !== null || isOpening}
                          leftIcon={<ClipboardList aria-hidden />}
                          title="Upload real study results and generate the Clinical Study Report (CSR)"
                          aria-label={`Study report for ${capitalize(r.drug_inn)}`}
                        >
                          Study report
                        </Button>
                      </div>
                    </Card>
                  )
                })}
              </ul>
            )}
          </TabPanel>
        </>
      ) : null}
    </div>
  )
}
