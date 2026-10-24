import { useAuth } from '@clerk/clerk-react'
import { authEnabled } from '../lib/auth'

function useClerkSignedIn(): boolean {
  const { isSignedIn } = useAuth()
  return Boolean(isSignedIn)
}

/** Auth is off (no Clerk key): there is no session to check. The launch API's
 *  signup() then just navigates into the app, which is open access. */
function useNoSession(): boolean {
  return false
}

/**
 * Whether the visitor already has a session, so the landing composer can go
 * straight into the app instead of opening the sign-up modal.
 *
 * `authEnabled` is a module-level constant (the env var never changes at
 * runtime), so choosing the hook once at import time keeps the hook order
 * stable across renders - and avoids calling Clerk's `useAuth` when there is
 * no <ClerkProvider> (auth off).
 */
export const useIsSignedIn: () => boolean = authEnabled ? useClerkSignedIn : useNoSession
