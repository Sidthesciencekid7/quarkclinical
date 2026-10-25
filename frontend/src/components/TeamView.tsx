import { useEffect, useState } from 'react'
import { api } from '../api/client'
import { useOrg } from '../lib/org'
import { useActingUser } from '../lib/actingUser'
import { ROLE_DISPLAY_NAMES } from '../types'
import type { OrgMember } from '../types'
import { Badge, Banner, Button, Card, CardHeader, Check, Copy, Field, Input, RefreshCw, Spinner } from '../ui'
import type { BadgeVariant } from '../ui'

const LICENSE_BADGE: Record<string, { label: string; variant: BadgeVariant }> = {
  active: { label: 'Active', variant: 'success' },
  pending: { label: 'Pending', variant: 'warning' },
  suspended: { label: 'Suspended', variant: 'danger' },
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
    setMembersError(null)
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
    } catch { /* clipboard unavailable: the user can still select and copy manually */ }
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
  const license = org.license ? (LICENSE_BADGE[org.license.status] ?? { label: org.license.status, variant: 'neutral' as BadgeVariant }) : null

  return (
    <div className="mx-auto w-full max-w-3xl space-y-6 px-4 py-8 sm:px-8 sm:py-10">
      <div>
        <h1 className="text-h1 text-fg">Team</h1>
        <div className="mt-2 flex flex-wrap items-center gap-2 text-body text-fg-muted">
          <span className="font-medium text-fg">{org.name}</span>
          {license ? (
            <>
              <Badge variant={license.variant} dot>
                License {license.label.toLowerCase()}
              </Badge>
              <span className="tabular-nums">
                {seatsUsed} of {seatCount} seats used
              </span>
            </>
          ) : (
            <span>No license on file</span>
          )}
        </div>
      </div>

      {org.is_owner ? (
        <Card as="section">
          <CardHeader
            title="Invite your team"
            subtitle={`Anyone with this link can join ${org.name} as a seat on your license.`}
            level={2}
          />
          {joinLink ? (
            <div className="mt-4 flex flex-wrap items-center gap-2">
              <div className="min-w-0 flex-1 basis-60">
                <Field label="Invite link" labelHidden>
                  <Input readOnly value={joinLink} onFocus={e => e.target.select()} className="font-mono text-small" />
                </Field>
              </div>
              <Button
                variant="secondary"
                onClick={handleCopy}
                leftIcon={copied ? <Check aria-hidden /> : <Copy aria-hidden />}
              >
                {copied ? 'Copied' : 'Copy link'}
              </Button>
              <span className="sr-only" role="status">
                {copied ? 'Invite link copied to the clipboard' : ''}
              </span>
            </div>
          ) : (
            <p className="mt-4 text-body text-fg-subtle">No active join link.</p>
          )}
          <div className="mt-3">
            <Button
              variant="ghost"
              size="sm"
              onClick={handleRotate}
              loading={rotating}
              leftIcon={<RefreshCw aria-hidden />}
            >
              {rotating ? 'Regenerating…' : 'Regenerate link'}
            </Button>
          </div>
          {error ? (
            <Banner tone="danger" className="mt-3">
              {error}
            </Banner>
          ) : null}
        </Card>
      ) : null}

      {!org.is_owner && !loading ? (
        <Banner tone="info">
          Only the organization owner can share or regenerate the invite link
          {owner ? (
            <>
              . Ask <span className="font-semibold">{owner.email}</span>
            </>
          ) : null}
          .
        </Banner>
      ) : null}

      <Card as="section" padding="none" aria-labelledby="members-heading" className="overflow-hidden">
        <div className="border-b border-line px-5 py-4">
          <h2 id="members-heading" className="text-h3 text-fg">
            Team members
          </h2>
        </div>
        {loading ? (
          <p className="flex items-center gap-2 px-5 py-4 text-body text-fg-subtle" role="status">
            <Spinner size={16} />
            Loading…
          </p>
        ) : membersError ? (
          <div className="p-4">
            <Banner tone="danger" title="Couldn't load team members">
              {membersError}
            </Banner>
          </div>
        ) : (
          <ul className="divide-y divide-[color:var(--border)]">
            {members.map(m => (
              <li key={m.id} className="flex items-center justify-between gap-3 px-5 py-3">
                <div className="min-w-0">
                  <p className="truncate text-body font-medium text-fg">{m.email}</p>
                  <p className="text-small text-fg-subtle">{ROLE_DISPLAY_NAMES[m.role]}</p>
                </div>
                {m.is_org_owner ? <Badge variant="info">Owner</Badge> : null}
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  )
}
