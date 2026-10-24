import { useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { Badge, Banner, Button, ButtonLink, Card, Mail, Spinner, Wordmark } from '../ui'
import { createChat } from '../api/chat'
import { AccountButton } from '../lib/auth'
import { clearPendingPrompt, loadPendingPrompt } from '../lib/pendingPrompt'
import type { PendingPrompt } from '../lib/pendingPrompt'
import { useOrg } from '../lib/org'
import { FEEDBACK_EMAIL } from '../shell/config'
import Onboarding from './Onboarding'

/** Full-page frame shared by the gate screens (pending, error, loading). */
function GateFrame({ children }: { children: ReactNode }) {
  return (
    <div className="app-canvas flex min-h-screen flex-col px-4 py-6">
      <div className="mx-auto flex w-full max-w-lg items-center justify-between">
        <Wordmark showMark />
        <AccountButton />
      </div>
      <div className="mx-auto flex w-full max-w-lg flex-1 items-center py-8">{children}</div>
    </div>
  )
}

/** Shown instead of the app while the org's license isn't active yet — a
 *  self-serve org starts "pending" and stays that way until the operator
 *  manually activates it (no Stripe; sales-assisted). Blocks the whole
 *  workspace rather than just protocol generation, since a pending owner
 *  also can't usefully invite teammates yet (join_org itself requires an
 *  active license — see routers/orgs.py).
 *
 *  If the visitor typed a study description on the landing page before
 *  signing up, it is shown here and sent to POST /api/chat as the first
 *  message of a new session (no AI call; no license needed). The browser copy
 *  is kept until that save succeeds, so a failed or missing endpoint never
 *  loses the text. */
function PendingLicenseScreen({ status }: { status: string }) {
  const suspended = status === 'suspended'
  // Read once: the text must stay on screen even after the browser copy is cleared.
  const [prompt] = useState<PendingPrompt | null>(() => (suspended ? null : loadPendingPrompt()))
  const [savedToWorkspace, setSavedToWorkspace] = useState(false)
  const attempted = useRef(false)

  useEffect(() => {
    if (!prompt || attempted.current) return
    attempted.current = true
    createChat({ kind: prompt.kind, initial_message: prompt.text })
      .then(() => {
        clearPendingPrompt()
        setSavedToWorkspace(true)
      })
      .catch(() => {
        /* keep the browser copy; it is retried the next time this screen opens */
      })
  }, [prompt])

  return (
    <GateFrame>
      <Card padding="lg" className="w-full">
        <Badge variant={suspended ? 'danger' : 'warning'} dot>
          {suspended ? 'License suspended' : 'License pending'}
        </Badge>
        <h1 className="mt-3 text-h2 text-fg">
          {suspended ? 'Your license is currently suspended' : 'We’re setting up your workspace'}
        </h1>
        <p className="mt-2 text-body text-fg-muted">
          {suspended
            ? 'Your organization’s license needs attention. Reach out to your account contact to resolve this.'
            : 'Your organization was created, but QuarkClinical is sold as a sales-assisted annual license. We’ll activate your account as soon as your sales conversation is confirmed — usually within one business day.'}
        </p>

        {prompt ? (
          <section aria-labelledby="saved-study-title" className="mt-6 rounded-card border border-line bg-surface-muted p-4">
            <h2 id="saved-study-title" className="text-h3 text-fg">
              Your study description is saved
            </h2>
            <p className="mt-0.5 text-small text-fg-muted">
              {savedToWorkspace
                ? 'It is saved in your workspace and will be waiting when your account is activated. Nothing has been sent to AI.'
                : 'It is saved in this browser and will be waiting when your account is activated. Nothing has been sent to AI.'}
            </p>
            <blockquote className="mt-3 max-h-44 overflow-y-auto whitespace-pre-wrap rounded-xl bg-surface px-3.5 py-3 text-body text-fg">
              {prompt.text}
            </blockquote>
          </section>
        ) : null}

        <div className="mt-6 flex flex-wrap items-center gap-2">
          <ButtonLink
            href={`mailto:${FEEDBACK_EMAIL}?subject=${encodeURIComponent('QuarkClinical account activation')}`}
            variant="secondary"
            leftIcon={<Mail />}
          >
            Contact us
          </ButtonLink>
        </div>
      </Card>
    </GateFrame>
  )
}

/** Shown when the org fetch fails for a reason other than "not provisioned"
 *  (network blip, 500, timeout, etc.) — surfaces the failure instead of
 *  silently falling through to the app with a null org. */
function OrgLoadErrorScreen({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <GateFrame>
      <Card padding="lg" className="w-full">
        <h1 className="text-h2 text-fg">Couldn’t load your organization</h1>
        <Banner tone="danger" className="mt-4">
          {message}
        </Banner>
        <div className="mt-5">
          <Button onClick={onRetry}>Retry</Button>
        </div>
      </Card>
    </GateFrame>
  )
}

/** Renders the onboarding (create/join org) screen, or a pending-activation
 *  screen, instead of children — depending on the signed-in account's org
 *  state. */
export default function OrgGate({ children }: { children: ReactNode }) {
  const { loading, needsOnboarding, org, orgLoadError, refresh } = useOrg()

  if (loading) {
    return (
      <div className="app-canvas flex min-h-screen items-center justify-center gap-2 text-body text-fg-muted" role="status" aria-live="polite">
        <Spinner size={18} />
        <span>Loading…</span>
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
