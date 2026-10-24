/**
 * Breadcrumbs for the app shell's top bar.
 *
 * The app uses <BrowserRouter> (not a data router), so route `handle` objects
 * are not available. Instead the trail is derived from the URL, and pages that
 * know a friendlier name for an id (the drug on `/app/protocols/:id`, the
 * title of a chat) publish it with `useSetCrumb`. Until a label arrives the
 * trail shows a neutral placeholder ("Protocol"), never the raw id.
 */
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import { useLocation } from 'react-router-dom'

export interface Crumb {
  label: string
  /** Absent on the last crumb (the current page). */
  to?: string
}

/** Keys for `useSetCrumb`; one namespace per entity so ids can never collide. */
export const crumbKey = {
  protocol: (id: string) => `protocol:${id}`,
  budget: (id: string) => `budget:${id}`,
  chat: (id: string) => `chat:${id}`,
}

/** Pure: the trail for a pathname. `labels` maps crumb keys to display names. */
export function crumbsForPath(pathname: string, labels: Record<string, string> = {}): Crumb[] {
  const path = pathname.replace(/\/+$/, '') || '/'
  const seg = path.split('/').filter(Boolean)
  if (seg[0] !== 'app') return []
  const [section, id, sub] = seg.slice(1)

  switch (section) {
    case undefined:
      return [{ label: 'Home' }]
    case 'chat':
      if (!id || id === 'new') return [{ label: 'New study' }]
      return [
        { label: 'New study', to: '/app/chat/new' },
        { label: labels[crumbKey.chat(id)] ?? 'Conversation' },
      ]
    case 'protocols': {
      if (!id) return [{ label: 'Protocols' }]
      const name = labels[crumbKey.protocol(id)] ?? 'Protocol'
      if (sub === 'report') {
        return [
          { label: 'Protocols', to: '/app/protocols' },
          { label: name, to: `/app/protocols/${id}` },
          { label: 'Study report' },
        ]
      }
      return [{ label: 'Protocols', to: '/app/protocols' }, { label: name }]
    }
    case 'budgets': {
      if (!id) return [{ label: 'Budgets' }]
      return [{ label: 'Budgets', to: '/app/budgets' }, { label: labels[crumbKey.budget(id)] ?? 'Budget' }]
    }
    case 'reports':
      return [{ label: 'Study reports' }]
    case 'team':
      return [{ label: 'Team' }]
    case 'protocol':
      return [{ label: 'New study', to: '/app/chat/new' }, { label: 'Protocol wizard' }]
    case 'budgeting':
      return [{ label: 'New study', to: '/app/chat/new' }, { label: 'Budget wizard' }]
    default:
      return [{ label: 'Home', to: '/app' }, { label: 'Page not found' }]
  }
}

interface BreadcrumbApi {
  labels: Record<string, string>
  setLabel: (key: string, label: string | null) => void
}

const BreadcrumbContext = createContext<BreadcrumbApi>({ labels: {}, setLabel: () => {} })

export function BreadcrumbProvider({ children }: { children: ReactNode }) {
  const [labels, setLabels] = useState<Record<string, string>>({})
  const setLabel = useCallback((key: string, label: string | null) => {
    setLabels(prev => {
      if (label === null) {
        if (!(key in prev)) return prev
        const { [key]: _removed, ...rest } = prev
        return rest
      }
      return prev[key] === label ? prev : { ...prev, [key]: label }
    })
  }, [])
  const value = useMemo(() => ({ labels, setLabel }), [labels, setLabel])
  return <BreadcrumbContext.Provider value={value}>{children}</BreadcrumbContext.Provider>
}

/** The current trail, from the URL plus any labels pages have published. */
export function useBreadcrumbs(): Crumb[] {
  const { pathname } = useLocation()
  const { labels } = useContext(BreadcrumbContext)
  return useMemo(() => crumbsForPath(pathname, labels), [pathname, labels])
}

/** Publish a display name for an entity id while the calling page is mounted. */
export function useSetCrumb(key: string | null | undefined, label: string | null | undefined): void {
  const { setLabel } = useContext(BreadcrumbContext)
  useEffect(() => {
    if (!key || !label) return
    setLabel(key, label)
    return () => setLabel(key, null)
  }, [key, label, setLabel])
}
