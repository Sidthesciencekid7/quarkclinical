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

/** App-header identity control. When identity is bound to the verified Clerk
 *  session it renders a static "Signed in as …" label; in local mode it's the
 *  free "Acting as" dropdown for role-switching demos. */
export function ActingUserPicker() {
  const { users, currentUser, setCurrentUserId, loading, bound, error } = useActingUser()

  if (loading) {
    return <span className="text-[11px] text-slate-400 font-medium">Loading users…</span>
  }
  if (error) {
    return (
      <span className="text-[11px] text-amber-400 font-medium" title={error}>
        Account not provisioned — ask an administrator
      </span>
    )
  }
  if (users.length === 0) {
    return (
      <span className="text-[11px] text-amber-400 font-medium">
        No users seeded — create/edit is disabled
      </span>
    )
  }
  if (bound && currentUser) {
    return (
      <span className="text-[11px] font-medium text-slate-300/80">
        Signed in as{' '}
        <span className="text-slate-100 font-semibold">
          {currentUser.email} — {ROLE_DISPLAY_NAMES[currentUser.role]}
        </span>
      </span>
    )
  }

  return (
    <label className="flex items-center gap-1.5 text-[11px] font-medium text-slate-300/80">
      Acting as
      <select
        value={currentUser?.id ?? ''}
        onChange={e => setCurrentUserId(e.target.value)}
        className="bg-white/5 border border-white/10 rounded-md px-1.5 py-1 text-slate-100 text-[11px] font-semibold focus:outline-none focus:ring-1 focus:ring-cyan-400/50 cursor-pointer"
      >
        {users.map(u => (
          <option key={u.id} value={u.id} className="text-slate-900">
            {u.email} — {ROLE_DISPLAY_NAMES[u.role]}
          </option>
        ))}
      </select>
    </label>
  )
}
