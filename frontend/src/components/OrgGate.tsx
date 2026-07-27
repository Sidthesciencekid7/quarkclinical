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
            : 'Your organization was created, but ProtocolAI is sold as a sales-assisted annual license. We’ll activate your account as soon as your sales conversation is confirmed — usually within one business day.'}
        </p>
      </div>
    </div>
  )
}

/** Renders the onboarding (create/join org) screen, or a pending-activation
 *  screen, instead of children — depending on the signed-in account's org
 *  state. */
export default function OrgGate({ children }: { children: ReactNode }) {
  const { loading, needsOnboarding, org } = useOrg()

  if (loading) {
    return (
      <div className="app-canvas min-h-screen flex items-center justify-center">
        <span className="text-sm text-slate-400 font-medium">Loading…</span>
      </div>
    )
  }

  if (needsOnboarding) return <Onboarding />

  if (org?.license && org.license.status !== 'active') {
    return <PendingLicenseScreen status={org.license.status} />
  }

  return <>{children}</>
}
