import { useEffect, useState } from 'react'
import { api } from '../api/client'
import { useActingUser } from '../lib/actingUser'
import type { ProtocolSummary, StudyType } from '../types'

interface Props {
  /** Open a past protocol in the editor (parent fetches full sections). */
  onOpen: (summary: ProtocolSummary) => void
  /** Start a new protocol (switch to the wizard). */
  onNew: () => void
  /** Open the Clinical Study Report (post-study analysis) flow for this protocol. */
  onOpenCsr: (summary: ProtocolSummary) => void
  /** The id currently being opened, so its card can show a spinner. */
  openingId: string | null
}

const STUDY_LABELS: Record<StudyType, string> = {
  BE: 'Bioequivalence',
  biosimilar_pk: 'Biosimilar PK',
  food_effect: 'Food Effect',
}

const STUDY_BADGE: Record<StudyType, string> = {
  BE: 'bg-cyan-50 text-cyan-700 border-cyan-200',
  biosimilar_pk: 'bg-violet-50 text-violet-700 border-violet-200',
  food_effect: 'bg-amber-50 text-amber-700 border-amber-200',
}

const STATUS_PILL: Record<string, string> = {
  draft: 'bg-slate-100 text-slate-600 border-slate-200',
  review: 'bg-amber-50 text-amber-700 border-amber-200',
  approved: 'bg-emerald-50 text-emerald-700 border-emerald-200',
}

function relativeDate(iso: string): string {
  const then = new Date(iso).getTime()
  const days = Math.floor((Date.now() - then) / 86_400_000)
  if (days <= 0) return 'Today'
  if (days === 1) return 'Yesterday'
  if (days < 30) return `${days} days ago`
  return new Date(iso).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })
}

function CopyIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 16 16" fill="none">
      <rect x="5.5" y="5.5" width="8" height="8" rx="1.5" stroke="currentColor" strokeWidth="1.5" />
      <path d="M10.5 3.5V3a1 1 0 00-1-1H3a1 1 0 00-1 1v6.5a1 1 0 001 1h.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  )
}

function ChartIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 16 16" fill="none">
      <path d="M2 14h12M4 14V8M8 14V3M12 14v-5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}

function Spinner({ size = 16 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none" className="animate-spin">
      <circle cx="8" cy="8" r="6" stroke="#e0f2fe" strokeWidth="2.5" />
      <path d="M8 2a6 6 0 016 6" stroke="#0891b2" strokeWidth="2.5" strokeLinecap="round" />
    </svg>
  )
}

function DocIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 20 20" fill="none">
      <path d="M4 3h9l4 4v11a1 1 0 01-1 1H4a1 1 0 01-1-1V4a1 1 0 011-1z" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
      <path d="M13 3v5h4M7 11h6M7 14h4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}

export default function ReportsView({ onOpen, onNew, onOpenCsr, openingId }: Props) {
  const [reports, setReports] = useState<ProtocolSummary[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  // Id of the protocol currently being duplicated. Doubles as the in-flight
  // lock: while it's non-null EVERY Duplicate button is disabled, so a
  // double-click (or a click on a second row) can't create two clones.
  const [cloningId, setCloningId] = useState<string | null>(null)
  const [cloneError, setCloneError] = useState<string | null>(null)

  // The reports list is org-scoped to the acting user, so we must wait until
  // identity is resolved before fetching — otherwise the request goes out with
  // no X-User-Id (and 401s) on a fresh browser where the picker hasn't set the
  // acting user yet. Re-fetch when the acting user changes.
  const { currentUser, loading: identityLoading } = useActingUser()

  useEffect(() => {
    if (identityLoading) return
    let cancelled = false
    setReports(null)
    setError(null)
    if (!currentUser) {
      setError('No user available — cannot load reports.')
      return
    }
    api.listProtocols()
      .then(list => { if (!cancelled) setReports(list) })
      .catch(e => { if (!cancelled) setError(e instanceof Error ? e.message : 'Failed to load reports') })
    return () => { cancelled = true }
  }, [identityLoading, currentUser?.id])

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

  return (
    <main className="app-canvas min-h-[calc(100vh-57px)]">
      <div className="max-w-4xl mx-auto px-5 py-10">
        {/* Header row */}
        <div className="flex items-end justify-between mb-6 flex-wrap gap-3">
          <div>
            <h1 className="text-2xl font-extrabold text-slate-900 tracking-tight">My Reports</h1>
            <p className="text-sm text-slate-500 mt-1">
              Every protocol your organization has generated. Open one to keep editing, export, or review.
            </p>
          </div>
          <button
            onClick={onNew}
            className="inline-flex items-center gap-2 px-4 py-2.5 rounded-xl bg-cyan-600 hover:bg-cyan-700 text-white text-sm font-bold shadow-sm shadow-cyan-600/20 transition-colors cursor-pointer"
          >
            <svg width="15" height="15" viewBox="0 0 15 15" fill="none">
              <path d="M7.5 2v11M2 7.5h11" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
            </svg>
            New Protocol
          </button>
        </div>

        {/* Loading */}
        {reports === null && !error && (
          <div className="space-y-3">
            {[0, 1, 2].map(i => (
              <div key={i} className="h-[76px] rounded-2xl border border-slate-200 bg-white/60 animate-pulse" />
            ))}
          </div>
        )}

        {/* Error */}
        {error && (
          <div className="rounded-2xl border border-red-200 bg-red-50 p-5 text-sm text-red-700">
            Couldn&apos;t load your reports: {error}
          </div>
        )}

        {/* Duplicate failed — non-fatal, the list itself is still usable */}
        {cloneError && (
          <div className="mb-3 rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
            Couldn&apos;t duplicate that protocol: {cloneError}
          </div>
        )}

        {/* Empty */}
        {reports?.length === 0 && (
          <div className="rounded-2xl border border-slate-200 bg-white p-12 text-center shadow-sm">
            <div className="w-14 h-14 rounded-2xl bg-cyan-50 border border-cyan-100 text-cyan-500 flex items-center justify-center mx-auto mb-4">
              <DocIcon />
            </div>
            <h3 className="text-lg font-bold text-slate-900">No protocols yet</h3>
            <p className="text-sm text-slate-500 mt-1.5 max-w-sm mx-auto">
              Generate your first bioequivalence or biosimilar PK protocol — it&apos;ll show up here so you can reopen it anytime.
            </p>
            <button
              onClick={onNew}
              className="mt-5 inline-flex items-center gap-2 px-5 py-2.5 rounded-xl bg-cyan-600 hover:bg-cyan-700 text-white text-sm font-bold cursor-pointer transition-colors"
            >
              Generate your first protocol
            </button>
          </div>
        )}

        {/* List */}
        {reports && reports.length > 0 && (
          <div className="space-y-2.5">
            {reports.map(r => {
              const isOpening = openingId === r.id
              const isCloning = cloningId === r.id
              return (
                // Row is a <div>, not a <button>: it holds two independent
                // actions (Open / Duplicate) and buttons cannot nest.
                <div
                  key={r.id}
                  className="section-card w-full flex items-center gap-4 px-5 py-4 group"
                >
                  <button
                    onClick={() => onOpen(r)}
                    disabled={isOpening || isCloning}
                    className="flex-1 min-w-0 text-left flex items-center gap-4 disabled:opacity-70 cursor-pointer"
                  >
                    <span className="flex-shrink-0 w-10 h-10 rounded-xl bg-gradient-to-br from-cyan-500 to-teal-600 text-white flex items-center justify-center shadow-sm shadow-cyan-600/25">
                      <DocIcon />
                    </span>

                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2 flex-wrap">
                        <span className="font-bold text-slate-900 capitalize truncate">{r.drug_inn}</span>
                        <span className={`text-[10px] font-bold border px-1.5 py-0.5 rounded-md ${STUDY_BADGE[r.study_type]}`}>
                          {STUDY_LABELS[r.study_type]}
                        </span>
                        <span className={`text-[10px] font-bold border px-1.5 py-0.5 rounded-md capitalize ${STATUS_PILL[r.status] ?? STATUS_PILL.draft}`}>
                          {r.status}
                        </span>
                        {r.guideline_outdated && (
                          <span className="text-[10px] font-bold border px-1.5 py-0.5 rounded-md bg-amber-50 text-amber-700 border-amber-200" title="Locked to a superseded guideline">
                            Guideline outdated
                          </span>
                        )}
                      </div>
                      <p className="text-xs text-slate-500 mt-1 font-medium truncate">
                        {r.guideline_version} · v{r.version} · {r.section_count} sections · {relativeDate(r.created_at)}
                      </p>
                    </div>

                    <span className="flex-shrink-0 text-slate-400 group-hover:text-cyan-600 transition-colors flex items-center gap-1.5 text-xs font-semibold">
                      {isOpening ? (
                        <Spinner />
                      ) : (
                        <>
                          Open
                          <svg width="14" height="14" viewBox="0 0 14 14" fill="none" className="transition-transform group-hover:translate-x-0.5">
                            <path d="M3 7h8M7 3l4 4-4 4" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" />
                          </svg>
                        </>
                      )}
                    </span>
                  </button>

                  {/* Start-from-template: server-side copy, no AI re-run. */}
                  <button
                    onClick={() => handleDuplicate(r)}
                    disabled={cloningId !== null || isOpening}
                    title="Duplicate as a new draft — reuses this study design, nothing is regenerated"
                    className="flex-shrink-0 inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg border border-slate-200 text-slate-500 text-xs font-semibold transition-colors cursor-pointer hover:text-cyan-700 hover:border-cyan-200 hover:bg-cyan-50 disabled:opacity-50 disabled:cursor-not-allowed disabled:hover:text-slate-500 disabled:hover:border-slate-200 disabled:hover:bg-transparent"
                  >
                    {isCloning ? <Spinner size={13} /> : <CopyIcon />}
                    {isCloning ? 'Duplicating…' : 'Duplicate'}
                  </button>

                  {/* Post-study: analyze real results → Clinical Study Report. */}
                  <button
                    onClick={() => onOpenCsr(r)}
                    disabled={cloningId !== null || isOpening}
                    title="Upload real study results and generate the Clinical Study Report (CSR)"
                    className="flex-shrink-0 inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg border border-slate-200 text-slate-500 text-xs font-semibold transition-colors cursor-pointer hover:text-teal-700 hover:border-teal-200 hover:bg-teal-50 disabled:opacity-50 disabled:cursor-not-allowed disabled:hover:text-slate-500 disabled:hover:border-slate-200 disabled:hover:bg-transparent"
                  >
                    <ChartIcon />
                    Study Report
                  </button>
                </div>
              )
            })}
          </div>
        )}
      </div>
    </main>
  )
}
