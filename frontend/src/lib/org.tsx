/**
 * Organization / license / team context.
 *
 * Resolves the signed-in account's org via GET /api/orgs/me. If the backend
 * reports the account isn't provisioned yet (a brand-new sign-in that hasn't
 * created or joined an org), `needsOnboarding` flips true so the app can show
 * the create/join screen instead of a dead end.
 */
import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { api } from '../api/client'
import { useActingUser } from './actingUser'
import type { Org } from '../types'

interface OrgApi {
  org: Org | null
  loading: boolean
  needsOnboarding: boolean
  orgLoadError: string | null
  refresh: () => Promise<void>
}

const OrgContext = createContext<OrgApi>({
  org: null,
  loading: true,
  needsOnboarding: false,
  orgLoadError: null,
  refresh: async () => {},
})

export const useOrg = () => useContext(OrgContext)

const NOT_PROVISIONED_HINT = 'not provisioned'

export function OrgProvider({ children }: { children: ReactNode }) {
  const { loading: actingUserLoading, currentUser } = useActingUser()
  const [org, setOrg] = useState<Org | null>(null)
  const [loading, setLoading] = useState(true)
  const [needsOnboarding, setNeedsOnboarding] = useState(false)
  const [orgLoadError, setOrgLoadError] = useState<string | null>(null)
  const latestRequestId = useRef(0)
  const orgUserIdRef = useRef<string | null>(null)

  const refresh = useCallback(async () => {
    const requestId = ++latestRequestId.current
    const requestUserId = currentUser?.id ?? null
    setLoading(true)
    setOrgLoadError(null)
    try {
      const mine = await api.getMyOrg()
      if (latestRequestId.current !== requestId) return
      setOrg(mine)
      setNeedsOnboarding(false)
      setOrgLoadError(null)
      orgUserIdRef.current = requestUserId
    } catch (e) {
      if (latestRequestId.current !== requestId) return
      const message = e instanceof Error ? e.message.toLowerCase() : ''
      if (message.includes(NOT_PROVISIONED_HINT)) {
        setOrg(null)
        setNeedsOnboarding(true)
        orgUserIdRef.current = requestUserId
      } else {
        setOrgLoadError(e instanceof Error ? e.message : 'Failed to load organization')
        if (orgUserIdRef.current !== requestUserId) {
          // Transient failure while resolving a DIFFERENT identity than the
          // one the cached `org` belongs to (e.g. just switched "acting as"
          // user) — don't keep rendering the previous identity's org.
          setOrg(null)
        }
        // else: same identity as the cached org — transient failure (e.g.
        // the rotate-join-token refresh in TeamView.tsx) — leave org as-is
        // rather than blocking the app on a transient error.
      }
    } finally {
      if (latestRequestId.current === requestId) setLoading(false)
    }
  }, [currentUser?.id])

  useEffect(() => {
    if (actingUserLoading) return
    refresh()
    // Re-resolve whenever the acting identity settles OR changes — in dev
    // mode (no Clerk) switching the "acting as" picker must re-fetch the new
    // identity's org, not keep showing the previous one's.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [actingUserLoading, currentUser?.id])

  return (
    <OrgContext.Provider value={{ org, loading, needsOnboarding, orgLoadError, refresh }}>
      {children}
    </OrgContext.Provider>
  )
}
