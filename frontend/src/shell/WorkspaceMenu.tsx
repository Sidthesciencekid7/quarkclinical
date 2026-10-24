import { useNavigate } from 'react-router-dom'
import { Badge, Check, ChevronsUpDown, LinkIcon, LogOut, Menu, Shield, Users } from '../ui'
import type { BadgeVariant, MenuEntry } from '../ui'
import { authEnabled } from '../lib/auth'
import { useActingUser } from '../lib/actingUser'
import { useOrg } from '../lib/org'
import { ROLE_DISPLAY_NAMES } from '../types'
import type { OrgLicense } from '../types'
import { initialOf } from './format'
import { useShell } from './ShellContext'
import { useSignOut } from './useSignOut'

const LICENSE_BADGE: Record<OrgLicense['status'], { label: string; variant: BadgeVariant }> = {
  active: { label: 'Active', variant: 'success' },
  pending: { label: 'Pending', variant: 'warning' },
  suspended: { label: 'Suspended', variant: 'danger' },
}

export function OrgAvatar({ name, size = 'md' }: { name: string; size?: 'sm' | 'md' }) {
  return (
    <span
      aria-hidden
      className={
        size === 'sm'
          ? 'flex h-6 w-6 flex-none items-center justify-center rounded-md bg-navy-900 text-[12px] font-semibold text-white'
          : 'flex h-8 w-8 flex-none items-center justify-center rounded-lg bg-navy-900 text-small font-semibold text-white'
      }
    >
      {initialOf(name)}
    </span>
  )
}

/**
 * The sidebar's workspace card: org avatar, name, license state and seats. It
 * is a workspace card, not an org switcher (a user belongs to exactly one org).
 * The menu holds Team & seats, the owner's invite link, Admin console, the dev
 * "Acting as" picker (auth off only) and Sign out.
 */
export function WorkspaceMenu({ rail }: { rail: boolean }) {
  const { org } = useOrg()
  const { users, currentUser, setCurrentUserId, bound } = useActingUser()
  const { capabilities, copyInviteLink } = useShell()
  const navigate = useNavigate()
  const signOut = useSignOut()

  const name = org?.name ?? 'Workspace'
  const license = org?.license ?? null
  const licenseBadge = license ? LICENSE_BADGE[license.status] : null
  const isOwner = capabilities?.is_org_owner ?? org?.is_owner ?? false
  const roleLabel = currentUser ? ROLE_DISPLAY_NAMES[currentUser.role] : null
  const seats = license ? `${license.seats_used}/${license.seat_count} seats` : null

  const items: MenuEntry[] = [
    { id: 'team', label: 'Team & seats', icon: <Users />, onSelect: () => navigate('/app/team') },
  ]
  if (isOwner) {
    items.push({ id: 'invite', label: 'Copy invite link', icon: <LinkIcon />, onSelect: () => void copyInviteLink() })
  }
  if (capabilities?.is_admin) {
    items.push({ id: 'admin', label: 'Admin console', icon: <Shield />, onSelect: () => navigate('/admin') })
  }
  if (!authEnabled && !bound && users.length > 0) {
    items.push({ type: 'separator', id: 'sep-acting' }, { type: 'label', id: 'label-acting', label: 'Acting as (dev)' })
    for (const u of users) {
      const current = u.id === currentUser?.id
      items.push({
        id: `acting-${u.id}`,
        label: `${u.email} (${ROLE_DISPLAY_NAMES[u.role]})`,
        trailing: current ? (
          <>
            <Check aria-hidden className="h-4 w-4 text-fg" />
            <span className="sr-only">current</span>
          </>
        ) : undefined,
        onSelect: () => setCurrentUserId(u.id),
      })
    }
  }
  if (signOut) {
    items.push({ type: 'separator', id: 'sep-signout' }, { id: 'signout', label: 'Sign out', icon: <LogOut />, onSelect: signOut })
  }

  const header = (
    <div className="min-w-0">
      <p className="truncate text-body font-semibold text-fg">{name}</p>
      <p className="mt-0.5 text-small text-fg-muted">
        {roleLabel ? `Your role: ${roleLabel}` : 'Signed in'}
        {isOwner ? ' · Organization owner' : ''}
      </p>
      {currentUser ? <p className="mt-0.5 truncate text-small text-fg-subtle">{currentUser.email}</p> : null}
      {licenseBadge ? (
        <p className="mt-2 flex items-center gap-2 text-small text-fg-muted">
          <Badge variant={licenseBadge.variant} dot className="h-5 px-2 text-[11px]">
            License {licenseBadge.label.toLowerCase()}
          </Badge>
          {seats ? <span className="tabular-nums">{seats}</span> : null}
        </p>
      ) : null}
    </div>
  )

  const trigger = rail ? (
    <button
      type="button"
      aria-label={`Workspace menu, ${name}`}
      title={name}
      className="mx-auto flex h-10 w-10 items-center justify-center rounded-xl border border-line bg-surface shadow-xs transition-colors duration-fast hover:bg-surface-hover"
    >
      <OrgAvatar name={name} size="sm" />
    </button>
  ) : (
    <button
      type="button"
      aria-label={`Workspace menu, ${name}`}
      className="flex w-full items-center gap-2.5 rounded-xl border border-line bg-surface p-2 text-left shadow-xs transition-colors duration-fast hover:bg-surface-hover"
    >
      <OrgAvatar name={name} />
      <span className="min-w-0 flex-1">
        <span className="block truncate text-body font-medium text-fg">{name}</span>
        {licenseBadge || seats ? (
          <span className="mt-0.5 flex items-center gap-1.5 text-small text-fg-subtle">
            {licenseBadge ? (
              <Badge variant={licenseBadge.variant} dot className="h-5 px-2 text-[11px]">
                {licenseBadge.label}
              </Badge>
            ) : null}
            {seats ? <span className="truncate tabular-nums">{seats}</span> : null}
          </span>
        ) : null}
      </span>
      <ChevronsUpDown aria-hidden className="h-4 w-4 flex-none text-fg-subtle" />
    </button>
  )

  return (
    <Menu
      trigger={trigger}
      items={items}
      header={header}
      label="Workspace menu"
      placement="bottom-start"
      minWidth={288}
    />
  )
}
