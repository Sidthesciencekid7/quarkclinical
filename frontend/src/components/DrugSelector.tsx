import { useEffect, useId, useRef, useState } from 'react'
import { api } from '../api/client'
import { Badge, Button, IconButton, Input, Pill, Plus, Search, Spinner, X, cx } from '../ui'
import type { Drug, DrugModality } from '../types'

interface Props {
  /** Currently chosen drug. When set, the selector shows it (with a clear button) instead of the search box. */
  value?: Drug | null
  /** Called with the picked drug, or null when the selection is cleared. */
  onChange: (drug: Drug | null) => void
  /** Label of each result's button (default "Use"), e.g. "Use" -> "Use adalimumab". */
  pickLabel?: string
  /**
   * Called when a valid drug name has no match. Adding a drug to the shared
   * catalogue is a confirm-card step in chat, so this only *asks* for that
   * (the assistant then offers the confirm card); the selector never creates a
   * drug itself. Omit to hide the offer.
   */
  onAddRequest?: (inn: string) => void
  disabled?: boolean
  autoFocus?: boolean
  className?: string
}

const INN_PATTERN = /^[a-zA-Z][a-zA-Z\s-]{1,63}$/
const isValidInn = (s: string) => INN_PATTERN.test(s.trim())

const MODALITY_LABELS: Record<DrugModality, string> = {
  biologic: 'Biologic',
  small_molecule: 'Small molecule',
  unknown: 'Unclassified',
}

/**
 * Drug search by INN. Results are listed inline (never in a portal), so it
 * works inside a scrolling chat card, a drawer or a modal without clipping.
 * Search starts at 2 characters with a 300 ms debounce.
 */
export default function DrugSelector({ value = null, onChange, pickLabel = 'Use', onAddRequest, disabled, autoFocus, className }: Props) {
  const [query, setQuery] = useState('')
  const [results, setResults] = useState<Drug[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [searched, setSearched] = useState(false)
  const searchId = useRef(0)
  const listId = useId()

  useEffect(() => {
    if (query.trim().length < 2) {
      searchId.current++
      setResults([])
      setSearched(false)
      setError(null)
      setLoading(false)
      return
    }
    const t = window.setTimeout(async () => {
      const mine = ++searchId.current
      setLoading(true)
      setError(null)
      try {
        const data = await api.searchDrugs(query.trim())
        if (mine !== searchId.current) return
        setResults(data)
        setSearched(true)
      } catch (e) {
        if (mine !== searchId.current) return
        setResults([])
        setSearched(true)
        setError(e instanceof Error ? e.message : 'Could not reach the server')
      } finally {
        if (mine === searchId.current) setLoading(false)
      }
    }, 300)
    return () => window.clearTimeout(t)
  }, [query])

  const pick = (drug: Drug) => {
    onChange(drug)
    setQuery('')
    setResults([])
    setSearched(false)
  }

  if (value) {
    return (
      <div className={cx('flex items-center gap-3 rounded-input border border-line bg-surface-muted p-3', className)}>
        <span aria-hidden className="flex h-9 w-9 flex-none items-center justify-center rounded-xl bg-surface text-fg-muted">
          <Pill className="h-4 w-4" />
        </span>
        <div className="min-w-0 flex-1">
          <p className="truncate text-body font-semibold capitalize text-fg">{value.inn}</p>
          {value.brand_names.length > 0 ? <p className="truncate text-small text-fg-muted">{value.brand_names.join(' · ')}</p> : null}
        </div>
        <IconButton label="Clear selected drug" icon={<X />} size="sm" disabled={disabled} onClick={() => onChange(null)} />
      </div>
    )
  }

  const trimmed = query.trim()
  const noMatch = searched && !loading && !error && results.length === 0

  return (
    <div className={className}>
      <Input
        shape="pill"
        type="search"
        autoComplete="off"
        autoFocus={autoFocus}
        disabled={disabled}
        value={query}
        onChange={e => setQuery(e.target.value)}
        onKeyDown={e => {
          if (e.key === 'Enter') {
            e.preventDefault()
            if (results.length > 0) pick(results[0])
          }
          if (e.key === 'Escape') setQuery('')
        }}
        placeholder="Search any drug by INN, e.g. ibuprofen"
        aria-label="Search drugs by INN"
        aria-controls={listId}
        leftIcon={loading ? <Spinner size={16} /> : <Search />}
      />

      <div id={listId} aria-live="polite">
        {error ? <p className="mt-2 text-small text-[color:var(--bad-fg)]">Search failed: {error}</p> : null}

        {results.length > 0 ? (
          <ul className="m-0 mt-2 max-h-64 list-none divide-y divide-line overflow-y-auto rounded-input border border-line p-0">
            {results.map(drug => (
              <li key={drug.id} className="flex items-center gap-3 px-3 py-2.5">
                <div className="min-w-0 flex-1">
                  <p className="truncate text-body font-semibold capitalize text-fg">{drug.inn}</p>
                  <p className="flex flex-wrap items-center gap-x-2 text-small text-fg-muted">
                    {drug.brand_names.length > 0 ? <span className="truncate">{drug.brand_names.join(' · ')}</span> : null}
                    {drug.modality && drug.modality !== 'unknown' ? <Badge variant="neutral">{MODALITY_LABELS[drug.modality]}</Badge> : null}
                  </p>
                </div>
                <Button size="sm" variant="secondary" disabled={disabled} onClick={() => pick(drug)} aria-label={`${pickLabel} ${drug.inn}`}>
                  {pickLabel}
                </Button>
              </li>
            ))}
          </ul>
        ) : null}

        {noMatch ? (
          <div className="mt-2 rounded-input border border-line px-3 py-2.5 text-small">
            <p className="text-fg-muted">No drug in the catalogue matches &ldquo;{trimmed}&rdquo;.</p>
            {!isValidInn(trimmed) ? (
              <p className="mt-1 text-[color:var(--warn-fg)]">A drug name is 2 to 64 letters, spaces or hyphens only.</p>
            ) : onAddRequest ? (
              <Button
                size="sm"
                variant="secondary"
                className="mt-2"
                leftIcon={<Plus />}
                disabled={disabled}
                onClick={() => onAddRequest(trimmed.toLowerCase())}
              >
                Ask to add &ldquo;{trimmed.toLowerCase()}&rdquo;
              </Button>
            ) : null}
          </div>
        ) : null}
      </div>
    </div>
  )
}
