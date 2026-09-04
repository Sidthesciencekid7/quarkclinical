import { useEffect, useMemo, useState } from 'react'
import { api } from '../api/client'
import { useActingUser } from '../lib/actingUser'
import { relativeDate } from './budgetFormat'
import type { ProtocolSummary, StudyType } from '../types'

interface Props {
  value: string | null
  onChange: (id: string | null, summary: ProtocolSummary | null) => void
  disabled?: boolean
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

/** Reusable protocol picker: the create-budget flow's "link a protocol" step
 *  and BudgetDerivationPanel's re-link control both render this directly. */
export default function BudgetProtocolPicker({ value, onChange, disabled }: Props) {
  const [protocols, setProtocols] = useState<ProtocolSummary[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [query, setQuery] = useState('')

  // Same identity gate as ReportsView/BudgetList — firing before the acting
  // user resolves means no X-User-Id and a 401 on a fresh browser.
  const { currentUser, loading: identityLoading, error: identityError } = useActingUser()

  useEffect(() => {
    if (identityLoading) return
    let cancelled = false
    setProtocols(null)
    setError(null)
    if (!currentUser) {
      setError(identityError ?? 'No user available — cannot load protocols.')
      return
    }
    api.listProtocols()
      .then(list => { if (!cancelled) setProtocols(list) })
      .catch(e => { if (!cancelled) setError(e instanceof Error ? e.message : 'Failed to load protocols') })
    return () => { cancelled = true }
  }, [identityLoading, currentUser?.id, identityError])

  const filtered = useMemo(() => {
    if (!protocols) return []
    const q = query.trim().toLowerCase()
    if (!q) return protocols
    return protocols.filter(p => p.drug_inn.toLowerCase().includes(q))
  }, [protocols, query])

  if (error) {
    return (
      <div className="rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
        Couldn&apos;t load protocols: {error}
      </div>
    )
  }

  if (protocols === null) {
    return (
      <div className="space-y-2">
        {[0, 1].map(i => (
          <div key={i} className="h-14 rounded-xl border border-slate-200 bg-white/60 animate-pulse" />
        ))}
      </div>
    )
  }

  if (protocols.length === 0) {
    return (
      <p className="text-sm text-slate-500 px-1 py-2">
        No protocols yet — your budget is already fully estimated without one.
      </p>
    )
  }

  return (
    <div className="space-y-2.5">
      <input
        type="text"
        value={query}
        onChange={e => setQuery(e.target.value)}
        placeholder="Search by drug name…"
        disabled={disabled}
        className="input-base"
      />
      <div role="radiogroup" aria-label="Select a protocol to link" className="max-h-72 overflow-y-auto space-y-2 pr-1">
        {filtered.length === 0 && (
          <p className="text-sm text-slate-400 px-1 py-2">No protocols match &ldquo;{query}&rdquo;.</p>
        )}
        {filtered.map(p => {
          const selected = value === p.id
          return (
            <button
              key={p.id}
              type="button"
              role="radio"
              aria-checked={selected}
              disabled={disabled}
              onClick={() => onChange(p.id, p)}
              className={`section-card w-full flex items-center gap-3 px-4 py-3 text-left cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-400/60 ${
                selected ? 'border-cyan-300 bg-cyan-50/50 ring-1 ring-cyan-200' : ''
              }`}
            >
              <span
                aria-hidden
                className={`flex-shrink-0 w-4 h-4 rounded-full border-2 flex items-center justify-center ${
                  selected ? 'border-cyan-500' : 'border-slate-300'
                }`}
              >
                {selected && <span className="w-2 h-2 rounded-full bg-cyan-500" />}
              </span>
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="font-bold text-slate-900 capitalize truncate">{p.drug_inn}</span>
                  <span className={`text-[10px] font-bold border px-1.5 py-0.5 rounded-md ${STUDY_BADGE[p.study_type]}`}>
                    {STUDY_LABELS[p.study_type]}
                  </span>
                </div>
                <p className="text-xs text-slate-500 mt-0.5 truncate">
                  v{p.version} · {p.section_count} sections · {relativeDate(p.created_at)}
                </p>
              </div>
            </button>
          )
        })}
      </div>
    </div>
  )
}
