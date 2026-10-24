import { useState } from 'react'
import { api } from '../api/client'
import { AccountButton, authEnabled } from '../lib/auth'
import { Banner, Button, Card, Field, Input, PillNav, Wordmark } from '../ui'

type Mode = 'create' | 'join'

const MODE_ITEMS = [
  { id: 'create', label: 'Create organization' },
  { id: 'join', label: 'Join a team' },
]

function extractToken(input: string): string {
  const trimmed = input.trim()
  try {
    const url = new URL(trimmed)
    const parts = url.pathname.split('/').filter(Boolean)
    return parts[parts.length - 1] || trimmed
  } catch {
    return trimmed
  }
}

export default function Onboarding() {
  const [mode, setMode] = useState<Mode>('create')
  const [name, setName] = useState('')
  const [token, setToken] = useState('')
  const [email, setEmail] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const emailArg = authEnabled ? undefined : email.trim()

  const handleCreate = async () => {
    setSubmitting(true)
    setError(null)
    try {
      await api.createOrg(name.trim(), emailArg)
      // Full reload, not just refresh() — ActingUserProvider resolved (and
      // cached) identity once at mount, before this account was provisioned,
      // and has no way to know to retry. A reload re-initializes every
      // provider (acting-user identity, org context) cleanly against the
      // now-provisioned account instead of leaving them stale.
      window.location.reload()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not create organization.')
      setSubmitting(false)
    }
  }

  const handleJoin = async () => {
    setSubmitting(true)
    setError(null)
    try {
      await api.joinOrg(extractToken(token), emailArg)
      window.location.reload()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not join that organization.')
      setSubmitting(false)
    }
  }

  const canSubmit =
    (mode === 'create' ? name.trim().length > 0 : token.trim().length > 0) &&
    (authEnabled || email.trim().length > 0)

  return (
    <div className="app-canvas flex min-h-screen flex-col px-4 py-6">
      <div className="mx-auto flex w-full max-w-md items-center justify-between">
        <Wordmark showMark />
        <AccountButton />
      </div>
      <div className="mx-auto flex w-full max-w-md flex-1 items-center py-8">
        <Card padding="lg" className="w-full">
          <h1 className="text-h2 text-fg">Set up your workspace</h1>
          <p className="mt-1 text-body text-fg-muted">
            Your account isn&rsquo;t part of an organization yet. Create a new one, or join an
            existing team with an invite link or code.
          </p>

          <PillNav
            ariaLabel="Create or join"
            items={MODE_ITEMS}
            value={mode}
            onChange={id => {
              setMode(id as Mode)
              setError(null)
            }}
            className="mt-5 flex w-full [&>button]:flex-1"
          />

          <form
            className="mt-5 flex flex-col gap-4"
            onSubmit={e => {
              e.preventDefault()
              if (submitting || !canSubmit) return
              void (mode === 'create' ? handleCreate() : handleJoin())
            }}
          >
            {!authEnabled && (
              <Field label="Your email" hint="Dev mode: there is no Clerk session to take it from.">
                <Input
                  type="email"
                  value={email}
                  onChange={e => setEmail(e.target.value)}
                  placeholder="you@company.com"
                  autoComplete="email"
                />
              </Field>
            )}

            {mode === 'create' ? (
              <Field label="Organization name">
                <Input
                  type="text"
                  value={name}
                  onChange={e => setName(e.target.value)}
                  placeholder="Acme Pharma"
                  autoComplete="organization"
                />
              </Field>
            ) : (
              <Field label="Invite link or code">
                <Input
                  type="text"
                  value={token}
                  onChange={e => setToken(e.target.value)}
                  placeholder="Paste the link or code your admin shared"
                />
              </Field>
            )}

            {error ? <Banner tone="danger">{error}</Banner> : null}

            <Button type="submit" size="lg" fullWidth loading={submitting} disabled={!canSubmit}>
              {submitting ? 'Working…' : mode === 'create' ? 'Create organization' : 'Join'}
            </Button>
          </form>
        </Card>
      </div>
    </div>
  )
}
