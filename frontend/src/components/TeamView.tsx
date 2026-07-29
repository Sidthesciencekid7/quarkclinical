import { useEffect, useState } from 'react'
import { api } from '../api/client'
import { useOrg } from '../lib/org'
import { useActingUser } from '../lib/actingUser'
import { ROLE_DISPLAY_NAMES } from '../types'
import type { OrgMember } from '../types'

function CopyIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
      <rect x="4.5" y="4.5" width="8" height="8" rx="1.5" stroke="currentColor" strokeWidth="1.3" />
      <path d="M2.5 9.5V2.5a1 1 0 011-1h7" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" />
    </svg>
  )
}

export default function TeamView() {
  const { org, refresh } = useOrg()
  const { currentUser } = useActingUser()
  const [members, setMembers] = useState<OrgMember[]>([])
  const [loading, setLoading] = useState(true)
  const [copied, setCopied] = useState(false)
  const [rotating, setRotating] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [membersError, setMembersError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setMembers([])
    api.listOrgMembers()
      .then(list => { if (!cancelled) setMembers(list) })
      .catch(e => { if (!cancelled) setMembersError(e instanceof Error ? e.message : 'Could not load team members.') })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [currentUser?.id])

  if (!org) return null

  const joinLink = org.join_token ? `${window.location.origin}/join/${org.join_token}` : null

  const handleCopy = async () => {
    if (!joinLink) return
    try {
      await navigator.clipboard.writeText(joinLink)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    } catch { /* clipboard unavailable — user can still select+copy manually */ }
  }

  const handleRotate = async () => {
    setRotating(true)
    setError(null)
    try {
      await api.rotateJoinToken()
      await refresh()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not rotate the join link.')
    } finally {
      setRotating(false)
    }
  }

  const seatsUsed = org.license?.seats_used ?? members.length
  const seatCount = org.license?.seat_count
  const owner = members.find(m => m.is_org_owner)

  return (
    <div className="max-w-2xl mx-auto px-4 py-10 space-y-6">
      <div>
        <h1 className="text-xl font-bold text-slate-900">{org.name}</h1>
        <p className="text-sm text-slate-500 mt-1">
          {org.license
            ? `License ${org.license.status} · ${seatsUsed} of ${seatCount} seats used`
            : 'No license on file'}
        </p>
      </div>

      {org.is_owner && (
        <div className="bg-white rounded-2xl border border-slate-200/80 shadow-sm p-6">
          <h2 className="text-sm font-bold text-slate-900 mb-1">Invite your team</h2>
          <p className="text-xs text-slate-500 mb-4">
            Anyone with this link can join {org.name} as a seat on your license.
          </p>
          {joinLink ? (
            <div className="flex items-center gap-2">
              <input
                readOnly
                value={joinLink}
                onFocus={e => e.target.select()}
                className="input-base flex-1 text-xs font-mono"
              />
              <button onClick={handleCopy} className="btn-ghost flex-shrink-0 flex items-center gap-1.5">
                <CopyIcon /> {copied ? 'Copied' : 'Copy'}
              </button>
            </div>
          ) : (
            <p className="text-xs text-slate-400">No active join link.</p>
          )}
          <button
            onClick={handleRotate}
            disabled={rotating}
            className="mt-3 text-xs font-semibold text-cyan-600 hover:text-cyan-700 disabled:opacity-50 cursor-pointer"
          >
            {rotating ? 'Regenerating…' : 'Regenerate link'}
          </button>
          {error && <p className="text-xs text-red-500 mt-2">{error}</p>}
        </div>
      )}

      {!org.is_owner && !loading && (
        <div className="bg-slate-50 border border-slate-200 rounded-2xl p-4">
          <p className="text-xs text-slate-500">
            Only the organization owner can share or regenerate the invite link
            {owner ? <> — that&rsquo;s <span className="font-semibold text-slate-700">{owner.email}</span></> : ''}.
          </p>
        </div>
      )}

      <div className="bg-white rounded-2xl border border-slate-200/80 shadow-sm overflow-hidden">
        <div className="px-6 py-4 border-b border-slate-100">
          <h2 className="text-sm font-bold text-slate-900">Team members</h2>
        </div>
        {loading ? (
          <p className="px-6 py-4 text-sm text-slate-400">Loading…</p>
        ) : membersError ? (
          <p className="px-6 py-4 text-sm text-red-500">Couldn&rsquo;t load team members: {membersError}</p>
        ) : (
          <ul className="divide-y divide-slate-100">
            {members.map(m => (
              <li key={m.id} className="px-6 py-3 flex items-center justify-between">
                <div>
                  <p className="text-sm font-semibold text-slate-900">{m.email}</p>
                  <p className="text-xs text-slate-400">{ROLE_DISPLAY_NAMES[m.role]}</p>
                </div>
                {m.is_org_owner && (
                  <span className="tag bg-cyan-50 text-cyan-700 border-cyan-200">Owner</span>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  )
}
