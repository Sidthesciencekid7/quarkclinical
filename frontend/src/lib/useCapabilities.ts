/**
 * What the signed-in user may do, for the shell (admin link, invite card...).
 *
 * Source of truth is GET /api/users/me/capabilities. If that call fails (an
 * older backend, a network blip) the hook falls back to what the org and the
 * resolved user already say: role and ownership are known locally, and
 * `is_admin` stays false because only the server can know it. `fromServer`
 * says which one you got.
 */
import { useEffect, useState } from 'react'
import { getCapabilities } from '../api/client'
import type { Capabilities } from '../api/client'
import { useActingUser } from './actingUser'
import { useOrg } from './org'

export interface CapabilitiesState {
  /** Null only while identity is still resolving. */
  capabilities: Capabilities | null
  loading: boolean
  /** True when the values came from the server rather than the local fallback. */
  fromServer: boolean
  error: string | null
}

export function useCapabilities(): CapabilitiesState {
  const { currentUser, loading: identityLoading } = useActingUser()
  const { org } = useOrg()
  const userId = currentUser?.id ?? null
  const [fetched, setFetched] = useState<{ userId: string; value: Capabilities } | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [settledFor, setSettledFor] = useState<string | null>(null)

  useEffect(() => {
    if (identityLoading || !userId) return
    let cancelled = false
    setError(null)
    getCapabilities()
      .then(value => {
        if (cancelled) return
        setFetched({ userId, value })
      })
      .catch(e => {
        if (cancelled) return
        setFetched(null)
        setError(e instanceof Error ? e.message : 'Could not load permissions.')
      })
      .finally(() => {
        if (!cancelled) setSettledFor(userId)
      })
    return () => {
      cancelled = true
    }
  }, [identityLoading, userId])

  if (identityLoading || !currentUser) {
    return { capabilities: null, loading: identityLoading, fromServer: false, error }
  }

  const loading = settledFor !== userId
  if (fetched && fetched.userId === userId) {
    return { capabilities: fetched.value, loading, fromServer: true, error: null }
  }

  const fallback: Capabilities = {
    role: currentUser.role,
    is_org_owner: !!org?.is_owner,
    is_admin: false,
    can_generate: currentUser.role === 'generator',
  }
  return { capabilities: fallback, loading, fromServer: false, error }
}
