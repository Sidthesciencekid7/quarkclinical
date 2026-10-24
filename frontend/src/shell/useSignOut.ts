import { useCallback } from 'react'
import { useClerk } from '@clerk/clerk-react'
import { authEnabled } from '../lib/auth'

function useClerkSignOut(): (() => void) | null {
  const clerk = useClerk()
  return useCallback(() => {
    void clerk.signOut({ redirectUrl: '/' })
  }, [clerk])
}

function useNoSignOut(): (() => void) | null {
  return null
}

/**
 * Sign-out handler, or null when auth is off (nothing to sign out of).
 * `authEnabled` is a module constant, so the same hook runs on every render.
 */
export const useSignOut: () => (() => void) | null = authEnabled ? useClerkSignOut : useNoSignOut
