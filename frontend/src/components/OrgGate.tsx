import type { ReactNode } from 'react'
import { useOrg } from '../lib/org'
import Onboarding from './Onboarding'

/** Shown instead of the app while the org's license isn't active yet — a
 *  self-serve org starts "pending" and stays that way until the operator
 *  manually activates it (no Stripe; sales-assisted). Blocks the whole
 *  workspace rather than just protocol generation, since a pending owner
 *  also can't usefully invite teammates yet (join_org itself requires an
 *  active license — see routers/orgs.py). */
function PendingLicenseScreen({ status }: { status: string }) {
  const suspended = status === 'suspended'
  return (
    <div className="app-canvas min-h-screen flex items-center justify-center px-4">
      <div className="bg-white rounded-2xl border border-slate-200/80 shadow-xl max-w-md w-full p-8 text-center">
        <h1 className="text-lg font-bold text-slate-900 mb-2">
          {suspended ? 'Your license is currently suspended' : 'We’re setting up your workspace'}
        </h1>
        <p className="text-sm text-slate-500 leading-relaxed">
          {suspended
            ? 'Your organization’s license needs attention. Reach out to your account contact to resolve this.'
            : 'Your organization was created, but Quark AI is sold as a sales-assisted annual license. We’ll activate your account as soon as your sales conversation is confirmed — usually within one business day.'}
        </p>
      </div>
    </div>
  )
}

/** Shown when the org fetch fails for a reason other than "not provisioned"
 *  (network blip, 500, timeout, etc.) — surfaces the failure instead of
 *  silently falling through to the app with a null org. */
function OrgLoadErrorScreen({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <div className="app-canvas min-h-screen flex items-center justify-center px-4">
      <div className="bg-white rounded-2xl border border-slate-200/80 shadow-xl max-w-md w-full p-8 text-center">
        <h1 className="text-lg font-bold text-slate-900 mb-2">Couldn’t load your organization</h1>
        <p className="text-sm text-slate-500 leading-relaxed mb-6">{message}</p>
        <button
          onClick={onRetry}
          className="inline-flex items-center justify-center rounded-lg bg-slate-900 text-white text-sm font-semibold px-4 py-2 hover:bg-slate-800"
        >
          Retry
        </button>
      </div>
    </div>
  )
}

/** Renders the onboarding (create/join org) screen, or a pending-activation
 *  screen, instead of children — depending on the signed-in account's org
 *  state. */
export default function OrgGate({ children }: { children: ReactNode }) {
  const { loading, needsOnboarding, org, orgLoadError, refresh } = useOrg()

  if (loading) {
    return (
      <div className="app-canvas min-h-screen flex items-center justify-center">
        <span className="text-sm text-slate-400 font-medium">Loading…</span>
      </div>
    )
  }

  if (orgLoadError) return <OrgLoadErrorScreen message={orgLoadError} onRetry={refresh} />

  if (needsOnboarding) return <Onboarding />

  if (org?.license && org.license.status !== 'active') {
    return <PendingLicenseScreen status={org.license.status} />
  }

  return <>{children}</>
}
