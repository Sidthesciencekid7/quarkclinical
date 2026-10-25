import { useEffect, useMemo, useState } from 'react'
import { api } from '../api/client'
import { useActingUser } from '../lib/actingUser'
import { Badge, Input, Search, cx } from '../ui'
import type { BadgeVariant } from '../ui'
import { relativeDate } from './budgetFormat'
import { studyTypeLabel } from '../lib/studyTypes'
import type { ProtocolSummary } from '../types'

/** One selectable protocol, independent of where the list came from. */
export interface ProtocolPickerItem {
  id: string
  drugInn: string
  /** `BE` | `biosimilar_pk` | `food_effect` (anything else is shown verbatim). */
  studyType: string
  /** Secondary line, e.g. "v2 · 17 sections · 3 days ago". */
  meta: string
}

const STUDY_BADGE: Record<string, BadgeVariant> = {
  BE: 'info',
  biosimilar_pk: 'neutral',
  food_effect: 'warning',
}

interface ListProps {
  items: ProtocolPickerItem[]
  value: string | null
  onChange: (id: string) => void
  disabled?: boolean
  /** Accessible name of the radio group. */
  ariaLabel?: string
  className?: string
}

/**
 * The searchable radio list of protocols. Presentational: callers supply the
 * items (the budget editor fetches them itself via `BudgetProtocolPicker`; the
 * chat budget card receives them from the server's card data).
 */
export function ProtocolPickerList({ items, value, onChange, disabled, ariaLabel = 'Select a protocol to link', className }: ListProps) {
  const [query, setQuery] = useState('')
  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    return q ? items.filter(p => p.drugInn.toLowerCase().includes(q)) : items
  }, [items, query])

  return (
    <div className={cx('space-y-2.5', className)}>
      {items.length > 5 ? (
        <Input
          type="search"
          leftIcon={<Search />}
          shape="pill"
          aria-label="Search protocols by drug name"
          value={query}
          onChange={e => setQuery(e.target.value)}
          placeholder="Search by drug name"
          disabled={disabled}
        />
      ) : null}
      <div role="radiogroup" aria-label={ariaLabel} className="max-h-72 space-y-2 overflow-y-auto pr-1">
        {filtered.length === 0 ? <p className="px-1 py-2 text-small text-fg-subtle">No protocols match &ldquo;{query}&rdquo;.</p> : null}
        {filtered.map(p => {
          const selected = value === p.id
          return (
            <button
              key={p.id}
              type="button"
              role="radio"
              aria-checked={selected}
              disabled={disabled}
              onClick={() => onChange(p.id)}
              className={cx(
                'flex w-full cursor-pointer items-center gap-3 rounded-input border px-4 py-3 text-left transition-colors duration-fast',
                'disabled:cursor-not-allowed disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-offset-2',
                selected ? 'border-accent-500 bg-[var(--info-bg)]' : 'border-line bg-surface hover:bg-surface-hover',
              )}
            >
              <span
                aria-hidden
                className={cx('flex h-4 w-4 flex-none items-center justify-center rounded-full border-2', selected ? 'border-accent-600' : 'border-line-strong')}
              >
                {selected ? <span className="h-2 w-2 rounded-full bg-accent-600" /> : null}
              </span>
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="truncate text-body font-semibold capitalize text-fg">{p.drugInn}</span>
                  <Badge variant={STUDY_BADGE[p.studyType] ?? 'neutral'}>{studyTypeLabel(p.studyType)}</Badge>
                </div>
                {p.meta ? <p className="mt-0.5 truncate text-small text-fg-muted">{p.meta}</p> : null}
              </div>
            </button>
          )
        })}
      </div>
    </div>
  )
}

interface Props {
  value: string | null
  onChange: (id: string | null, summary: ProtocolSummary | null) => void
  disabled?: boolean
}

/** Reusable protocol picker for the budget editor's "link a protocol" control:
 *  fetches the org's protocols and renders them with `ProtocolPickerList`. */
export default function BudgetProtocolPicker({ value, onChange, disabled }: Props) {
  const [protocols, setProtocols] = useState<ProtocolSummary[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  // Same identity gate as the other lists: firing before the acting user
  // resolves means no X-User-Id and a 401 on a fresh browser.
  const { currentUser, loading: identityLoading, error: identityError } = useActingUser()

  useEffect(() => {
    if (identityLoading) return
    let cancelled = false
    setProtocols(null)
    setError(null)
    if (!currentUser) {
      setError(identityError ?? 'No user available, so protocols cannot be loaded.')
      return
    }
    api
      .listProtocols()
      .then(list => {
        if (!cancelled) setProtocols(list)
      })
      .catch(e => {
        if (!cancelled) setError(e instanceof Error ? e.message : 'Failed to load protocols')
      })
    return () => {
      cancelled = true
    }
  }, [identityLoading, currentUser?.id, identityError])

  const items = useMemo<ProtocolPickerItem[]>(
    () =>
      (protocols ?? []).map(p => ({
        id: p.id,
        drugInn: p.drug_inn,
        studyType: p.study_type,
        meta: `v${p.version} · ${p.section_count} sections · ${relativeDate(p.created_at)}`,
      })),
    [protocols],
  )

  if (error) {
    return (
      <div role="alert" className="rounded-input border border-[color:var(--bad-border)] bg-[var(--bad-bg)] px-4 py-3 text-small text-[color:var(--bad-fg)]">
        Couldn&apos;t load protocols: {error}
      </div>
    )
  }

  if (protocols === null) {
    return (
      <div className="space-y-2" aria-busy="true">
        {[0, 1].map(i => (
          <div key={i} className="h-14 animate-pulse rounded-input border border-line bg-surface-muted" />
        ))}
      </div>
    )
  }

  if (protocols.length === 0) {
    return <p className="px-1 py-2 text-small text-fg-muted">No protocols yet. Your budget is already fully estimated without one.</p>
  }

  return (
    <ProtocolPickerList
      items={items}
      value={value}
      disabled={disabled}
      onChange={id => onChange(id, protocols.find(p => p.id === id) ?? null)}
    />
  )
}
