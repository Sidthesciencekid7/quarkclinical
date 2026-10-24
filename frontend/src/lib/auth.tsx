/**
 * Auth layer (Clerk) with graceful degradation.
 *
 * If VITE_CLERK_PUBLISHABLE_KEY is set, full Clerk auth activates: sign-in/up
 * modals, a protected /app route, and an account button. If the key is absent,
 * everything falls back to open access (buttons just route to /app) so the app
 * runs without any auth configuration.
 *
 * `authEnabled` is a module-level constant (the env var never changes at
 * runtime), so the conditional provider/component selection below is stable
 * across renders and does not violate the rules of hooks.
 */
import { createContext, useContext } from 'react'
import type { ReactNode } from 'react'
import { useNavigate, useLocation } from 'react-router-dom'
import {
  SignedIn,
  SignedOut,
  UserButton,
  useClerk,
} from '@clerk/clerk-react'

export const CLERK_KEY = import.meta.env.VITE_CLERK_PUBLISHABLE_KEY as string | undefined
export const authEnabled = Boolean(CLERK_KEY)

/* ---- launch (login / signup) API exposed to the landing page ---- */

/** Where Clerk (or the plain-mode navigate) sends the user after auth. */
const DEFAULT_LAUNCH_TARGET = '/app'

/** Accepts only a same-origin, root-relative path ("/app/chat/new?from=landing")
 *  and returns it unchanged; anything else (a non-string, "https://evil.test",
 *  protocol-relative "//evil.test", "/\evil.test", control characters or
 *  spaces, a path that resolves off-origin) returns null so the caller falls
 *  back to its default. Guards against open-redirect through the post-auth
 *  redirect. NOTE: handlers wired as `onClick={login}` pass a MouseEvent as the
 *  first argument, so non-strings must be tolerated, not thrown on. */
export function sanitizeLaunchTarget(target: unknown): string | null {
  if (typeof target !== 'string' || target.length === 0 || target.length > 2048) return null
  if (!target.startsWith('/') || target.startsWith('//') || target.startsWith('/\\')) return null
  if (/[\u0000-\u0020\u007f\\]/.test(target)) return null
  try {
    const base = typeof window !== 'undefined' ? window.location.origin : 'http://localhost'
    if (new URL(target, base).origin !== base) return null
  } catch {
    return null
  }
  return target
}

interface LaunchApi {
  /** `target`: optional same-origin path (starting with '/') to land on after auth. */
  login: (target?: string) => void
  signup: (target?: string) => void
}

const LaunchContext = createContext<LaunchApi>({
  login: () => {},
  signup: () => {},
})

export const useLaunch = () => useContext(LaunchContext)

function AuthedLaunchProvider({ children }: { children: ReactNode }) {
  const clerk = useClerk()
  const navigate = useNavigate()
  const location = useLocation()

  // Round-trip back to whatever protected route the user was actually trying
  // to reach (e.g. /join/:token) after auth completes. The one exception is
  // the marketing landing page itself ('/'), whose own login/signup buttons
  // have always intentionally sent new/returning users into the app shell.
  const defaultRedirectTarget =
    location.pathname === '/' ? DEFAULT_LAUNCH_TARGET : `${location.pathname}${location.search}`

  // Defensive: only call Clerk's modal openers when Clerk has actually loaded
  // and the method exists. If Clerk failed to load (blocked script, dev-instance
  // limit, a stale tab pointing at a restarted dev server, etc.) the click must
  // still DO something — fall back to navigating to redirectTarget (the Protected
  // route renders the full-page sign-in) rather than silently doing nothing.
  const open = (which: 'signIn' | 'signUp', target?: unknown) => {
    // An explicit, valid `target` wins; otherwise behave exactly as before.
    const redirectTarget = sanitizeLaunchTarget(target) ?? defaultRedirectTarget
    try {
      const fn = which === 'signIn' ? clerk?.openSignIn : clerk?.openSignUp
      if (clerk?.loaded && typeof fn === 'function') {
        fn.call(clerk, { forceRedirectUrl: redirectTarget })
        return
      }
    } catch (err) {
      console.error('Clerk modal failed to open; navigating to /app instead.', err)
    }
    navigate(redirectTarget)
  }

  const api: LaunchApi = {
    login: (target) => open('signIn', target),
    signup: (target) => open('signUp', target),
  }
  return <LaunchContext.Provider value={api}>{children}</LaunchContext.Provider>
}

function PlainLaunchProvider({ children }: { children: ReactNode }) {
  const navigate = useNavigate()
  const api: LaunchApi = {
    login: (target) => navigate(sanitizeLaunchTarget(target) ?? DEFAULT_LAUNCH_TARGET),
    signup: (target) => navigate(sanitizeLaunchTarget(target) ?? DEFAULT_LAUNCH_TARGET),
  }
  return <LaunchContext.Provider value={api}>{children}</LaunchContext.Provider>
}

/** Chosen once at load — must sit inside <BrowserRouter>. */
export const LaunchProvider = authEnabled ? AuthedLaunchProvider : PlainLaunchProvider

/* ---- route protection ---- */

/** Wraps a route element. With auth on: shows a sign-in/sign-up gate to
 *  signed-out visitors and the children to signed-in ones. With auth off:
 *  passes children straight through.
 *
 *  Deliberately does NOT embed Clerk's <SignIn> component directly — its own
 *  footer "Sign up" link hands off to Clerk's separate hosted Account Portal
 *  domain, and completion there is redirected per Clerk's DASHBOARD path
 *  settings, not this component's forceRedirectUrl prop — a real bug found
 *  in testing: it landed new users back on the marketing homepage instead of
 *  /app. Routing both directions through the MODAL API instead (same
 *  useLaunch() functions the landing page's own buttons use) keeps the whole
 *  flow client-side same-origin, where forceRedirectUrl is honored. */
export function Protected({ children }: { children: ReactNode }) {
  if (!authEnabled) return <>{children}</>
  return (
    <>
      <SignedIn>{children}</SignedIn>
      <SignedOut>
        <SignInGate />
      </SignedOut>
    </>
  )
}

function SignInGate() {
  const { login, signup } = useLaunch()
  return (
    <div
      className="min-h-screen grid place-items-center px-4 py-10"
      style={{ background: 'radial-gradient(120% 80% at 50% 0%, #122a52 0%, #0B1E3D 60%)' }}
    >
      <div className="text-center max-w-sm">
        <h1 className="text-white text-2xl font-bold tracking-tight mb-2">Sign in to continue</h1>
        <p className="text-slate-400 text-sm mb-8">You need an account to access QuarkClinical.</p>
        <div className="flex flex-col gap-3">
          <button
            onClick={() => login()}
            className="w-full py-3 rounded-xl font-bold text-sm text-white bg-gradient-to-r from-cyan-500 to-teal-500 shadow-lg shadow-cyan-500/30 cursor-pointer"
          >
            Log in
          </button>
          <button
            onClick={() => signup()}
            className="w-full py-3 rounded-xl font-bold text-sm text-white bg-white/10 border border-white/15 hover:bg-white/15 transition-colors cursor-pointer"
          >
            Sign up
          </button>
        </div>
      </div>
    </div>
  )
}

/** Dedicated gate for /admin — deliberately separate from Protected/SignInGate:
 *  offers only "Log in", never "Sign up", since nobody should be creating a
 *  fresh account to reach this page. The actual authorization boundary is
 *  still the backend's ADMIN_EMAILS check (unspoofable — derived from the
 *  verified Clerk session server-side); this only hardens the front door so
 *  it doesn't read as a public entry point. */
export function AdminProtected({ children }: { children: ReactNode }) {
  if (!authEnabled) return <>{children}</>
  return (
    <>
      <SignedIn>{children}</SignedIn>
      <SignedOut>
        <AdminSignInGate />
      </SignedOut>
    </>
  )
}

function AdminSignInGate() {
  const clerk = useClerk()
  const navigate = useNavigate()

  const handleLogin = () => {
    try {
      if (clerk?.loaded && typeof clerk.openSignIn === 'function') {
        clerk.openSignIn({ forceRedirectUrl: '/admin' })
        return
      }
    } catch (err) {
      console.error('Clerk modal failed to open; navigating to /admin instead.', err)
    }
    navigate('/admin')
  }

  return (
    <div
      className="min-h-screen grid place-items-center px-4 py-10"
      style={{ background: 'radial-gradient(120% 80% at 50% 0%, #122a52 0%, #0B1E3D 60%)' }}
    >
      <div className="text-center max-w-sm">
        <h1 className="text-white text-2xl font-bold tracking-tight mb-2">Admin sign in</h1>
        <p className="text-slate-400 text-sm mb-8">Restricted — authorized operators only.</p>
        <button
          onClick={handleLogin}
          className="w-full py-3 rounded-xl font-bold text-sm text-white bg-gradient-to-r from-cyan-500 to-teal-500 shadow-lg shadow-cyan-500/30 cursor-pointer"
        >
          Log in
        </button>
      </div>
    </div>
  )
}

/** Account avatar / sign-out menu. Renders nothing when auth is disabled. */
export function AccountButton() {
  if (!authEnabled) return null
  return (
    <UserButton
      afterSignOutUrl="/"
      appearance={{ elements: { avatarBox: 'w-8 h-8' } }}
    />
  )
}
