import type { ReactNode } from 'react'
import { Link, useLocation } from 'react-router-dom'
import { Badge, IconButton, MessageSquare, Shield, UserPlus, Wordmark, X, cx } from '../ui'
import QuarkLogo from '../components/QuarkLogo'
import { useOrg } from '../lib/org'
import { CHAT_KIND_LABELS } from './chatList'
import { MAIN_NAV, PINNED, isMainNavActive, isPinnedActive, pinnedHref } from './nav'
import { useShell } from './ShellContext'
import { WorkspaceMenu } from './WorkspaceMenu'

const ICON_BOX = 'flex h-5 w-5 flex-none items-center justify-center [&>svg]:h-[18px] [&>svg]:w-[18px]'

interface SideLinkProps {
  to: string
  label: string
  icon: ReactNode
  active: boolean
  rail: boolean
  badge?: ReactNode
  onNavigate?: () => void
  /** Secondary line under the label (recent chats). */
  hint?: string
}

function SideLink({ to, label, icon, active, rail, badge, onNavigate, hint }: SideLinkProps) {
  return (
    <Link
      to={to}
      onClick={onNavigate}
      aria-current={active ? 'page' : undefined}
      title={rail ? label : undefined}
      className={cx(
        'flex items-center gap-3 rounded-xl text-body no-underline transition-colors duration-fast',
        hint && !rail ? 'min-h-[44px] py-1.5' : 'h-9',
        rail ? 'mx-auto w-10 justify-center' : 'px-3',
        active
          ? 'bg-ink-100 font-medium text-fg'
          : 'text-fg-muted hover:bg-surface-hover hover:text-fg',
      )}
    >
      <span className={ICON_BOX}>{icon}</span>
      {rail ? (
        <span className="sr-only">{label}</span>
      ) : (
        <>
          <span className="min-w-0 flex-1">
            <span className="block truncate">{label}</span>
            {hint ? <span className="block truncate text-small font-normal text-fg-subtle">{hint}</span> : null}
          </span>
          {badge}
        </>
      )}
    </Link>
  )
}

function SectionLabel({ rail, children }: { rail: boolean; children: string }) {
  if (rail) return <div role="separator" className="mx-3 my-3 h-px bg-line" />
  return <h2 className="px-3 pb-1 pt-5 text-small font-medium text-fg-subtle">{children}</h2>
}

export interface SidebarProps {
  /** Render as the 64px icon rail. */
  rail: boolean
  /** Rendered inside the mobile drawer: shows a close button. */
  onClose?: () => void
  /** Called when a link is followed (the drawer closes itself on navigation). */
  onNavigate?: () => void
}

/** The sidebar's content. The shell wraps it in an <aside> or the mobile drawer. */
export function Sidebar({ rail, onClose, onNavigate }: SidebarProps) {
  const { pathname, search } = useLocation()
  const { org } = useOrg()
  const { capabilities, recentChats, copyInviteLink } = useShell()

  const license = org?.license ?? null
  const isOwner = capabilities?.is_org_owner ?? org?.is_owner ?? false
  const seatsRemain = !!license && license.seats_used < license.seat_count
  const showInvite = isOwner && seatsRemain
  const recentItems = recentChats?.available ? recentChats.items.slice(0, 5) : null

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className={cx('flex h-14 flex-none items-center', rail ? 'justify-center' : 'justify-between pl-5 pr-3')}>
        <Link
          to="/app"
          onClick={onNavigate}
          aria-label="QuarkClinical home"
          className="flex items-center rounded-lg"
        >
          {rail ? <QuarkLogo size={24} className="text-ink-950" /> : <Wordmark />}
        </Link>
        {onClose ? <IconButton label="Close navigation" icon={<X />} size="sm" onClick={onClose} /> : null}
      </div>

      <div className="flex-none px-3 pb-2 pt-1">
        <WorkspaceMenu rail={rail} />
      </div>

      <nav aria-label="Main" className="flex-none px-3 py-1">
        <ul className="flex flex-col gap-0.5">
          {MAIN_NAV.map(item => (
            <li key={item.id}>
              <SideLink
                to={item.to}
                label={item.label}
                icon={item.icon}
                active={isMainNavActive(item.id, pathname, search)}
                rail={rail}
                onNavigate={onNavigate}
                badge={
                  item.isNew ? (
                    <Badge variant="solid" className="h-5 px-2 text-[11px]">
                      New
                    </Badge>
                  ) : undefined
                }
              />
            </li>
          ))}
        </ul>
      </nav>

      <div className="min-h-0 flex-1 overflow-y-auto px-3 pb-3">
        <nav aria-label="Pinned">
          <SectionLabel rail={rail}>Pinned</SectionLabel>
          <ul className="flex flex-col gap-0.5">
            {PINNED.map(item => (
              <li key={item.id}>
                <SideLink
                  to={pinnedHref(item)}
                  label={item.label}
                  icon={item.icon}
                  active={isPinnedActive(item, pathname, search)}
                  rail={rail}
                  onNavigate={onNavigate}
                />
              </li>
            ))}
          </ul>
        </nav>

        {recentItems ? (
          <nav aria-label="Recent conversations">
            <SectionLabel rail={rail}>Recent</SectionLabel>
            {recentItems.length === 0 ? (
              rail ? null : <p className="px-3 py-1 text-small text-fg-subtle">No conversations yet</p>
            ) : (
              <ul className="flex flex-col gap-0.5">
                {recentItems.map(chat => (
                  <li key={chat.id}>
                    <SideLink
                      to={`/app/chat/${chat.id}`}
                      label={chat.title?.trim() || 'Untitled conversation'}
                      hint={CHAT_KIND_LABELS[chat.kind] ?? undefined}
                      icon={<MessageSquare />}
                      active={pathname === `/app/chat/${chat.id}`}
                      rail={rail}
                      onNavigate={onNavigate}
                    />
                  </li>
                ))}
              </ul>
            )}
          </nav>
        ) : null}
      </div>

      <div className="flex flex-none flex-col gap-2 px-3 pb-3 pt-2">
        {showInvite ? (
          rail ? (
            <IconButton
              label="Invite team members"
              icon={<UserPlus />}
              variant="secondary"
              onClick={() => void copyInviteLink()}
              className="mx-auto"
            />
          ) : (
            <button
              type="button"
              onClick={() => void copyInviteLink()}
              className="flex w-full flex-col items-start gap-1 rounded-card border border-line bg-surface p-3 text-left shadow-xs transition-colors duration-fast hover:bg-surface-hover"
            >
              <span aria-hidden className="mb-1 flex h-8 w-8 items-center justify-center rounded-full bg-surface-muted text-fg-muted [&>svg]:h-4 [&>svg]:w-4">
                <UserPlus />
              </span>
              <span className="text-body font-medium text-fg">Invite team members</span>
              <span className="text-small text-fg-muted">
                Copies your invite link so teammates can join your workspace.
              </span>
            </button>
          )
        ) : null}
        {capabilities?.is_admin ? (
          <SideLink
            to="/admin"
            label="Admin console"
            icon={<Shield />}
            active={pathname === '/admin'}
            rail={rail}
            onNavigate={onNavigate}
          />
        ) : null}
      </div>
    </div>
  )
}
