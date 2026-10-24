/**
 * "Acting as" identity — a stand-in for real per-user login.
 *
 * Clerk (see auth.tsx) authenticates the browser session, but the backend's
 * users table (org_id/email/role) isn't linked to Clerk identities yet (see
 * B1/B2 audit notes). Until that linkage exists, this lets an operator pick
 * which seeded user they're acting as; api/client.ts attaches that user's id
 * as the X-User-Id header on every mutating request, which the backend uses
 * for both audit-trail attribution and role checks (Author/Reviewer/Approver).
 */
import { createContext, useContext, useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import { api } from '../api/client'
import { authEnabled } from './auth'
import { ROLE_DISPLAY_NAMES } from '../types'
import { Badge, ChevronDown } from '../ui'
import type { AppUser } from '../types'

const STORAGE_KEY = 'protocolai_acting_user_id'

/** Synchronous read used by api/client.ts — must not depend on React state. */
export function getActingUserId(): string | null {
  try {
    return localStorage.getItem(STORAGE_KEY)
  } catch {
    return null
  }
}

function setActingUserId(id: string | null) {
  try {
    if (id) localStorage.setItem(STORAGE_KEY, id)
    else localStorage.removeItem(STORAGE_KEY)
  } catch {
    /* localStorage unavailable (private mode, etc.) — acting-as just won't persist */
  }
}

interface ActingUserApi {
  users: AppUser[]
  currentUser: AppUser | null
  setCurrentUserId: (id: string) => void
  loading: boolean
  /** True when identity is fixed by the verified Clerk session (auth on) and
   *  can't be freely switched — the picker renders read-only in this mode. */
  bound: boolean
  /** Set when auth is on but the signed-in account isn't provisioned. */
  error: string | null
}

const ActingUserContext = createContext<ActingUserApi>({
  users: [],
  currentUser: null,
  setCurrentUserId: () => {},
  loading: true,
  bound: false,
  error: null,
})

export const useActingUser = () => useContext(ActingUserContext)

export function ActingUserProvider({ children }: { children: ReactNode }) {
  const [users, setUsers] = useState<AppUser[]>([])
  const [currentUserId, setCurrentUserIdState] = useState<string | null>(getActingUserId())
  const [loading, setLoading] = useState(true)
  const [bound, setBound] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false

    async function init() {
      if (authEnabled) {
        // Clerk auth on: identity is the backend-resolved (verified) user.
        // The "acting as" picker cannot override it — the backend ignores the
        // X-User-Id header in this mode (see dependencies.authenticate_actor).
        try {
          const me = await api.getCurrentUser()
          if (cancelled) return
          setUsers([me])
          setBound(true)
          setCurrentUserIdState(me.id)
          setActingUserId(me.id)  // still sent as an audit hint; ignored for auth
        } catch (e) {
          if (cancelled) return
          setError(e instanceof Error ? e.message : 'Could not resolve your account.')
          setUsers([])
        } finally {
          if (!cancelled) setLoading(false)
        }
        return
      }

      // Local / no-Clerk mode: free "acting as" picker for role-switching demos.
      try {
        const list = await api.listUsers()
        if (cancelled) return
        setUsers(list)
        const stored = getActingUserId()
        const stillValid = stored && list.some(u => u.id === stored)
        const resolved = stillValid ? stored : (list[0]?.id ?? null)
        setCurrentUserIdState(resolved)
        setActingUserId(resolved)
      } catch {
        /* ignore — picker will show the empty state */
      } finally {
        if (!cancelled) setLoading(false)
      }
    }

    init()
    return () => { cancelled = true }
  }, [])

  const setCurrentUserId = (id: string) => {
    if (bound) return  // identity is session-bound; switching is disabled
    setCurrentUserIdState(id)
    setActingUserId(id)
  }

  const currentUser = users.find(u => u.id === currentUserId) ?? null

  return (
    <ActingUserContext.Provider value={{ users, currentUser, setCurrentUserId, loading, bound, error }}>
      {children}
    </ActingUserContext.Provider>
  )
}

/** Identity control. When identity is bound to the verified Clerk session it
 *  renders a static "Signed in as ..." pill; in local mode it is the free
 *  "Acting as" pill-select for role-switching demos. In the app shell the same
 *  switch lives in the workspace menu (shell/WorkspaceMenu.tsx); this component
 *  remains for the classic headers and renders on its own white pill, so it
 *  reads on both light and navy backgrounds. */
export function ActingUserPicker() {
  const { users, currentUser, setCurrentUserId, loading, bound, error } = useActingUser()

  if (loading) {
    return <Badge variant="neutral">Loading users…</Badge>
  }
  if (error) {
    return (
      <Badge variant="warning" title={error}>
        Account not provisioned — ask an administrator
      </Badge>
    )
  }
  if (users.length === 0) {
    return <Badge variant="warning">No users seeded — create/edit is disabled</Badge>
  }
  if (bound && currentUser) {
    return (
      <Badge variant="neutral" title={currentUser.email} className="max-w-full">
        <span className="truncate">
          Signed in as {currentUser.email} — {ROLE_DISPLAY_NAMES[currentUser.role]}
        </span>
      </Badge>
    )
  }

  return (
    <label className="relative inline-flex h-8 max-w-full items-center gap-1.5 rounded-pill border border-ink-200 bg-white pl-3 pr-2 text-small font-medium text-ink-500 shadow-xs">
      <span className="flex-none">Acting as</span>
      <select
        value={currentUser?.id ?? ''}
        onChange={e => setCurrentUserId(e.target.value)}
        className="min-w-0 cursor-pointer appearance-none rounded-pill bg-transparent py-1 pl-1 pr-6 text-small font-semibold text-ink-950 outline-none focus-visible:ring-2 focus-visible:ring-accent-500"
      >
        {users.map(u => (
          <option key={u.id} value={u.id}>
            {u.email} — {ROLE_DISPLAY_NAMES[u.role]}
          </option>
        ))}
      </select>
      <ChevronDown aria-hidden className="pointer-events-none absolute right-2.5 h-3.5 w-3.5 text-ink-500" />
    </label>
  )
}
