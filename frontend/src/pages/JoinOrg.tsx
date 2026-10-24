import { useCallback, useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { api } from '../api/client'
import { authEnabled } from '../lib/auth'
import { Banner, Button, Card, Field, Input, Spinner, Wordmark } from '../ui'

export default function JoinOrg() {
  const { token } = useParams<{ token: string }>()
  const navigate = useNavigate()
  const [email, setEmail] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const submit = useCallback(async () => {
    if (!token) return
    setSubmitting(true)
    setError(null)
    try {
      await api.joinOrg(token, authEnabled ? undefined : email.trim())
      navigate('/app', { replace: true })
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not join this organization.')
      setSubmitting(false)
    }
  }, [token, email, navigate])

  // Clerk mode: identity is already known from the session — join immediately.
  useEffect(() => {
    if (authEnabled) submit()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  if (!token) {
    return (
      <div className="app-canvas flex min-h-screen items-center justify-center px-4">
        <Banner tone="warning" title="Missing invite token">
          This invite link is incomplete. Ask your team owner for a fresh link.
        </Banner>
      </div>
    )
  }

  return (
    <div className="app-canvas flex min-h-screen flex-col items-center justify-center px-4 py-10">
      <Wordmark showMark size="lg" className="mb-6" />
      <Card padding="lg" className="w-full max-w-md">
        <h1 className="text-h2 text-fg">Joining your team&hellip;</h1>

        {!authEnabled && (
          <form
            className="mt-4 flex flex-col gap-4"
            onSubmit={e => {
              e.preventDefault()
              if (!submitting && email.trim()) void submit()
            }}
          >
            <Field label="Your email" hint="Dev mode: there is no Clerk session to take it from.">
              <Input
                type="email"
                value={email}
                onChange={e => setEmail(e.target.value)}
                placeholder="you@company.com"
                autoComplete="email"
              />
            </Field>
            <Button type="submit" size="lg" fullWidth loading={submitting} disabled={!email.trim()}>
              {submitting ? 'Joining…' : 'Join'}
            </Button>
          </form>
        )}

        {authEnabled && submitting && (
          <p className="mt-3 flex items-center gap-2 text-body text-fg-muted" role="status">
            <Spinner size={16} />
            Please wait&hellip;
          </p>
        )}
        {error ? <Banner tone="danger" className="mt-4">{error}</Banner> : null}
      </Card>
    </div>
  )
}
