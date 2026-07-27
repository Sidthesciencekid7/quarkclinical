/**
 * Organization / license / team context.
 *
 * Resolves the signed-in account's org via GET /api/orgs/me. If the backend
 * reports the account isn't provisioned yet (a brand-new sign-in that hasn't
 * created or joined an org), `needsOnboarding` flips true so the app can show
 * the create/join screen instead of a dead end.
 */
import { createContext, useCallback, useContext, useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import { api } from '../api/client'
import { useActingUser } from './actingUser'
import type { Org } from '../types'

interface OrgApi {
  org: Org | null
  loading: boolean
  needsOnboarding: boolean
  refresh: () => Promise<void>
}

const OrgContext = createContext<OrgApi>({
  org: null,
  loading: true,
  needsOnboarding: false,
  refresh: async () => {},
})

export const useOrg = () => useContext(OrgContext)

const NOT_PROVISIONED_HINT = 'not provisioned'

export function OrgProvider({ children }: { children: ReactNode }) {
  const { loading: actingUserLoading, currentUser } = useActingUser()
  const [org, setOrg] = useState<Org | null>(null)
  const [loading, setLoading] = useState(true)
  const [needsOnboarding, setNeedsOnboarding] = useState(false)

  const refresh = useCallback(async () => {
    setLoading(true)
    try {
      const mine = await api.getMyOrg()
      setOrg(mine)
      setNeedsOnboarding(false)
    } catch (e) {
      const message = e instanceof Error ? e.message.toLowerCase() : ''
      if (message.includes(NOT_PROVISIONED_HINT)) {
        setOrg(null)
        setNeedsOnboarding(true)
      }
      // Any other failure (network blip, etc.) — leave state as-is rather
      // than blocking the app on a transient error.
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    if (actingUserLoading) return
    refresh()
    // Re-resolve whenever the acting identity settles OR changes — in dev
    // mode (no Clerk) switching the "acting as" picker must re-fetch the new
    // identity's org, not keep showing the previous one's.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [actingUserLoading, currentUser?.id])

  return (
    <OrgContext.Provider value={{ org, loading, needsOnboarding, refresh }}>
      {children}
    </OrgContext.Provider>
  )
}
