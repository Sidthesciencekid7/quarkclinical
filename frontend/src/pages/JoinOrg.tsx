import { useCallback, useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { api } from '../api/client'
import { authEnabled } from '../lib/auth'

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
      <div className="app-canvas min-h-screen flex items-center justify-center px-4">
        <p className="text-sm text-slate-500">Missing invite token.</p>
      </div>
    )
  }

  return (
    <div className="app-canvas min-h-screen flex items-center justify-center px-4">
      <div className="bg-white rounded-2xl border border-slate-200/80 shadow-xl max-w-md w-full p-8 text-center">
        <h1 className="text-lg font-bold text-slate-900 mb-2">Joining your team&hellip;</h1>

        {!authEnabled && (
          <div className="text-left mt-4">
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
            <button
              type="button"
              onClick={submit}
              disabled={submitting || !email.trim()}
              className="mt-3 w-full py-3 rounded-xl font-bold text-sm text-white bg-cyan-600 hover:bg-cyan-700 disabled:opacity-50 cursor-pointer"
            >
              {submitting ? 'Joining…' : 'Join'}
            </button>
          </div>
        )}

        {authEnabled && submitting && <p className="text-sm text-slate-500 mt-2">Please wait&hellip;</p>}
        {error && <p className="text-xs text-red-500 mt-3">{error}</p>}
      </div>
    </div>
  )
}
