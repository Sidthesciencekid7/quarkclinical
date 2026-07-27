import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { api } from '../api/client'
import type { Drug } from '../types'

interface Props {
  value: Drug | null
  onChange: (drug: Drug | null) => void
}

function SearchIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
      <circle cx="6.5" cy="6.5" r="4.5" stroke="currentColor" strokeWidth="1.5" />
      <path d="M10 10l3 3" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  )
}

function SpinnerIcon({ color = '#0891b2' }: { color?: string }) {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" className="animate-spin">
      <circle cx="8" cy="8" r="6" stroke="#e2e8f0" strokeWidth="2.5" />
      <path d="M8 2a6 6 0 016 6" stroke={color} strokeWidth="2.5" strokeLinecap="round" />
    </svg>
  )
}

function XIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 13 13" fill="none">
      <path d="M2.5 2.5l8 8M10.5 2.5l-8 8" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" />
    </svg>
  )
}

function PlusIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 13 13" fill="none">
      <path d="M6.5 2v9M2 6.5h9" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
    </svg>
  )
}

export default function DrugSelector({ value, onChange }: Props) {
  const [query, setQuery] = useState('')
  const [results, setResults] = useState<Drug[]>([])
  const [isLoading, setIsLoading] = useState(false)
  const [isAdding, setIsAdding] = useState(false)
  const [addError, setAddError] = useState<string | null>(null)
  const [fetchError, setFetchError] = useState<string | null>(null)
  const [open, setOpen] = useState(false)
  const [noResults, setNoResults] = useState(false)
  const containerRef = useRef<HTMLDivElement>(null)
  const menuRef = useRef<HTMLDivElement>(null)
  const [menuRect, setMenuRect] = useState<{ top: number; left: number; width: number } | null>(null)

  const menuVisible = (open && results.length > 0) || (noResults && !isLoading)

  // The results dropdown is portaled to <body> (see render) so it can never be
  // clipped or covered by an ancestor's overflow/stacking context — e.g. the
  // wizard's Back/Continue button row, which sits right below this card and
  // was visually swallowing the bottom of the dropdown when it was positioned
  // as a normal in-flow-relative child. Position is computed from the input
  // row's real screen coordinates and kept in sync while open.
  useLayoutEffect(() => {
    if (!menuVisible) return
    const updateRect = () => {
      const el = containerRef.current
      if (!el) return
      const rect = el.getBoundingClientRect()
      setMenuRect({ top: rect.bottom + 8, left: rect.left, width: rect.width })
    }
    updateRect()
    window.addEventListener('scroll', updateRect, true)
    window.addEventListener('resize', updateRect)
    return () => {
      window.removeEventListener('scroll', updateRect, true)
      window.removeEventListener('resize', updateRect)
    }
  }, [menuVisible])

  useEffect(() => {
    if (query.length < 2) {
      setResults([])
      setOpen(false)
      setNoResults(false)
      return
    }
    setNoResults(false)
    setFetchError(null)
    const t = window.setTimeout(async () => {
      setIsLoading(true)
      try {
        const data = await api.searchDrugs(query)
        setResults(data)
        setOpen(data.length > 0)
        setNoResults(data.length === 0)
      } catch (err) {
        setResults([])
        setOpen(false)
        setNoResults(true)
        setFetchError(err instanceof Error ? err.message : 'Could not reach backend')
      } finally {
        setIsLoading(false)
      }
    }, 300)
    return () => clearTimeout(t)
  }, [query])

  useEffect(() => {
    function handle(e: MouseEvent) {
      const target = e.target as Node
      const insideInput = containerRef.current?.contains(target)
      const insideMenu = menuRef.current?.contains(target)
      if (!insideInput && !insideMenu) {
        setOpen(false)
        setNoResults(false)
      }
    }
    document.addEventListener('mousedown', handle)
    return () => document.removeEventListener('mousedown', handle)
  }, [])

  const handleSelect = (drug: Drug) => {
    onChange(drug)
    setQuery('')
    setOpen(false)
    setNoResults(false)
    setAddError(null)
    setFetchError(null)
    setResults([])
  }

  const handleAdd = async () => {
    setIsAdding(true)
    setAddError(null)
    try {
      const drug = await api.createDrug(query)
      handleSelect(drug)
    } catch (err) {
      setAddError(err instanceof Error ? err.message : 'Failed to add drug')
    } finally {
      setIsAdding(false)
    }
  }

  if (value) {
    return (
      <div className="flex items-center gap-4 p-4 bg-cyan-50 border border-cyan-200 rounded-xl">
        <div className="w-10 h-10 bg-cyan-100 rounded-xl flex items-center justify-center flex-shrink-0 border border-cyan-200">
          <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
            <rect x="2" y="7" width="14" height="4" rx="2" stroke="#0891b2" strokeWidth="1.5" />
            <path d="M7 9h4" stroke="#0891b2" strokeWidth="1.5" strokeLinecap="round" />
            <path d="M9 7V5M9 11v2" stroke="#0891b2" strokeWidth="1.5" strokeLinecap="round" />
          </svg>
        </div>
        <div className="flex-1 min-w-0">
          <p className="text-base font-bold text-slate-900 capitalize">{value.inn}</p>
          {value.brand_names.length > 0 && (
            <p className="text-xs text-cyan-600 font-medium mt-0.5">{value.brand_names.join(' · ')}</p>
          )}
          {value.rxnorm_id && (
            <p className="text-xs text-slate-400 mt-0.5 font-mono">RxNorm: {value.rxnorm_id}</p>
          )}
        </div>
        <button
          onClick={() => onChange(null)}
          className="p-1.5 text-slate-400 hover:text-slate-600 hover:bg-white rounded-lg transition-colors"
          aria-label="Clear selection"
        >
          <XIcon />
        </button>
      </div>
    )
  }

  return (
    <div ref={containerRef} className="relative">
      <div className="relative">
        <span className="absolute left-3.5 top-1/2 -translate-y-1/2 text-slate-400 pointer-events-none">
          {isLoading ? <SpinnerIcon /> : <SearchIcon />}
        </span>
        <input
          type="text"
          value={query}
          onChange={e => setQuery(e.target.value)}
          onFocus={() => { if (results.length > 0) setOpen(true) }}
          onKeyDown={e => {
            if (e.key === 'Enter') {
              e.preventDefault()
              if (results.length > 0) handleSelect(results[0])
              else if (noResults && query.trim().length >= 2) handleAdd()
            }
            if (e.key === 'Escape') { setOpen(false); setNoResults(false) }
          }}
          placeholder="Search any drug by INN — e.g. ibuprofen, semaglutide…"
          className="input-base pl-10"
          autoComplete="off"
        />
      </div>

      <p className="text-xs text-slate-400 mt-2 leading-relaxed">
        Type any INN — if not in the database, you can add it on the spot.
      </p>

      {menuVisible && menuRect && createPortal(
        <div
          ref={menuRef}
          style={{ position: 'fixed', top: menuRect.top, left: menuRect.left, width: menuRect.width, zIndex: 9999 }}
        >
          {open && results.length > 0 && (
            <ul className="bg-white border border-slate-200 rounded-xl shadow-xl max-h-64 overflow-y-auto divide-y divide-slate-100">
              {results.map(drug => (
                <li key={drug.id}>
                  <button
                    type="button"
                    onClick={() => handleSelect(drug)}
                    className="w-full text-left px-4 py-3 hover:bg-cyan-50 transition-colors first:rounded-t-xl last:rounded-b-xl cursor-pointer"
                  >
                    <p className="text-sm font-semibold text-slate-900 capitalize">{drug.inn}</p>
                    {drug.brand_names.length > 0 && (
                      <p className="text-xs text-slate-500 mt-0.5">{drug.brand_names.join(' · ')}</p>
                    )}
                    {drug.rxnorm_id && (
                      <p className="text-xs text-slate-400 mt-0.5 font-mono">RxNorm: {drug.rxnorm_id}</p>
                    )}
                  </button>
                </li>
              ))}
            </ul>
          )}

          {noResults && !isLoading && (
            <div className="bg-white border border-slate-200 rounded-xl shadow-xl p-5 text-center">
              {fetchError ? (
                <p className="text-sm text-red-500 font-medium">Backend error: {fetchError}</p>
              ) : (
                <>
                  <p className="text-sm text-slate-600 font-medium">
                    No results for &ldquo;{query}&rdquo;
                  </p>
                  <p className="text-xs text-slate-400 mt-0.5">Add it to the database to continue</p>
                </>
              )}
              {addError && <p className="text-xs text-red-500 mt-1">{addError}</p>}
              <button
                type="button"
                onClick={handleAdd}
                disabled={isAdding}
                className="mt-3 inline-flex items-center gap-2 px-4 py-2 bg-cyan-600 hover:bg-cyan-700 disabled:opacity-60 text-white text-sm font-semibold rounded-lg transition-colors cursor-pointer"
              >
                {isAdding ? <SpinnerIcon color="white" /> : <PlusIcon />}
                {isAdding ? 'Adding…' : `Add "${query.trim().toLowerCase()}"`}
              </button>
            </div>
          )}
        </div>,
        document.body
      )}
    </div>
  )
}
