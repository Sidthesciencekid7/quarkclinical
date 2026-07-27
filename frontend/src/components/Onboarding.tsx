import { useState } from 'react'
import { api } from '../api/client'
import { authEnabled } from '../lib/auth'

type Mode = 'create' | 'join'

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
    <div className="app-canvas min-h-screen flex items-center justify-center px-4">
      <div className="bg-white rounded-2xl border border-slate-200/80 shadow-xl max-w-md w-full p-8">
        <h1 className="text-lg font-bold text-slate-900 mb-1">Set up your workspace</h1>
        <p className="text-sm text-slate-500 mb-6 leading-relaxed">
          Your account isn&rsquo;t part of an organization yet. Create a new one, or join an
          existing team with an invite link or code.
        </p>

        <div className="flex rounded-xl border border-slate-200 p-1 mb-6 bg-slate-50">
          <button
            type="button"
            onClick={() => { setMode('create'); setError(null) }}
            className={[
              'flex-1 text-sm font-semibold py-2 rounded-lg transition-colors cursor-pointer',
              mode === 'create' ? 'bg-white shadow-sm text-slate-900' : 'text-slate-500',
            ].join(' ')}
          >
            Create organization
          </button>
          <button
            type="button"
            onClick={() => { setMode('join'); setError(null) }}
            className={[
              'flex-1 text-sm font-semibold py-2 rounded-lg transition-colors cursor-pointer',
              mode === 'join' ? 'bg-white shadow-sm text-slate-900' : 'text-slate-500',
            ].join(' ')}
          >
            Join a team
          </button>
        </div>

        {!authEnabled && (
          <div className="mb-4">
            <label className="block text-xs font-semibold text-slate-500 mb-1.5">
              Your email (dev mode — no Clerk session to derive it from)
            </label>
            <input
              type="email"
              value={email}
              onChange={e => setEmail(e.target.value)}
              placeholder="you@company.com"
              className="input-base"
            />
          </div>
        )}

        {mode === 'create' ? (
          <div className="mb-2">
            <label className="block text-xs font-semibold text-slate-500 mb-1.5">Organization name</label>
            <input
              type="text"
              value={name}
              onChange={e => setName(e.target.value)}
              placeholder="Acme Pharma"
              className="input-base"
            />
          </div>
        ) : (
          <div className="mb-2">
            <label className="block text-xs font-semibold text-slate-500 mb-1.5">Invite link or code</label>
            <input
              type="text"
              value={token}
              onChange={e => setToken(e.target.value)}
              placeholder="Paste the link or code your admin shared"
              className="input-base"
            />
          </div>
        )}

        {error && <p className="text-xs text-red-500 mt-2">{error}</p>}

        <button
          type="button"
          onClick={mode === 'create' ? handleCreate : handleJoin}
          disabled={submitting || !canSubmit}
          className="mt-5 w-full py-3 rounded-xl font-bold text-sm text-white bg-cyan-600 hover:bg-cyan-700 disabled:opacity-50 transition-colors cursor-pointer"
        >
          {submitting ? 'Working…' : mode === 'create' ? 'Create organization' : 'Join'}
        </button>
      </div>
    </div>
  )
}
