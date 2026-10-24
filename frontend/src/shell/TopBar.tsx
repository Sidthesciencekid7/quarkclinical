import { Link } from 'react-router-dom'
import {
  ButtonLink,
  ChevronRight,
  IconButton,
  MenuIcon,
  PanelLeft,
  PanelLeftClose,
  PanelLeftOpen,
  Sparkles,
  cx,
} from '../ui'
import { AccountButton, authEnabled } from '../lib/auth'
import { useBreadcrumbs } from '../lib/breadcrumbs'
import { FEEDBACK_HREF, NEW_CHAT_PATH } from './config'
import { useShell } from './ShellContext'

export function Breadcrumbs() {
  const crumbs = useBreadcrumbs()
  if (crumbs.length === 0) return null
  return (
    <nav aria-label="Breadcrumb" className="min-w-0">
      <ol className="flex min-w-0 items-center gap-1.5 text-body">
        {crumbs.map((crumb, i) => {
          const last = i === crumbs.length - 1
          return (
            <li key={`${i}-${crumb.label}`} className={cx('flex min-w-0 items-center gap-1.5', !last && 'flex-none max-sm:hidden')}>
              {crumb.to && !last ? (
                <Link
                  to={crumb.to}
                  className="truncate rounded-md text-fg-muted no-underline transition-colors duration-fast hover:text-fg"
                >
                  {crumb.label}
                </Link>
              ) : (
                <span
                  className={cx('truncate', last ? 'font-medium text-fg' : 'text-fg-muted')}
                  aria-current={last ? 'page' : undefined}
                >
                  {crumb.label}
                </span>
              )}
              {!last ? <ChevronRight aria-hidden className="h-3.5 w-3.5 flex-none text-fg-subtle" /> : null}
            </li>
          )
        })}
      </ol>
    </nav>
  )
}

/** Top bar: collapse / hamburger, breadcrumbs, then Feedback, Ask, activity dot, account. */
export function TopBar() {
  const { isMd, isLg, collapsed, toggleCollapsed, openDrawer, drawerOpen, generating } = useShell()

  // Below 768 it is a hamburger; between 768 and 1024 the sidebar is forced to
  // the icon rail, so the same button opens the full navigation as a drawer;
  // from 1024 up it collapses / expands the sidebar.
  let toggle
  if (!isMd) {
    toggle = (
      <IconButton
        label="Open navigation"
        icon={<MenuIcon />}
        onClick={e => openDrawer(e.currentTarget)}
        aria-haspopup="dialog"
        aria-expanded={drawerOpen}
      />
    )
  } else if (!isLg) {
    toggle = (
      <IconButton
        label="Open navigation"
        icon={<PanelLeft />}
        onClick={e => openDrawer(e.currentTarget)}
        aria-haspopup="dialog"
        aria-expanded={drawerOpen}
      />
    )
  } else {
    toggle = (
      <IconButton
        label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
        icon={collapsed ? <PanelLeftOpen /> : <PanelLeftClose />}
        onClick={toggleCollapsed}
        aria-expanded={!collapsed}
        aria-controls="app-sidebar"
      />
    )
  }

  return (
    <header className="flex h-14 flex-none items-center gap-2 border-b border-line bg-surface px-3 sm:px-4">
      {toggle}
      <div className="min-w-0 flex-1 pl-1">
        <Breadcrumbs />
      </div>

      <div className="flex flex-none items-center gap-2">
        {generating ? (
          <Link
            to="/app/protocols"
            aria-label="A protocol is being generated. Open Protocols."
            title="A protocol is being generated"
            className="relative flex h-9 w-9 items-center justify-center rounded-full transition-colors duration-fast hover:bg-surface-hover"
          >
            <span aria-hidden className="absolute h-2.5 w-2.5 animate-ping rounded-full bg-accent-400 opacity-70" />
            <span aria-hidden className="relative h-2.5 w-2.5 rounded-full bg-accent-600" data-testid="generation-dot" />
          </Link>
        ) : null}
        <ButtonLink as="a" href={FEEDBACK_HREF} variant="secondary" size="sm" className="max-sm:hidden">
          Feedback
        </ButtonLink>
        <ButtonLink
          as={Link}
          to={NEW_CHAT_PATH}
          variant="secondary"
          size="sm"
          leftIcon={<Sparkles />}
          aria-label="Ask"
        >
          <span className="max-sm:hidden">Ask</span>
        </ButtonLink>
        {authEnabled ? (
          <div className="flex h-9 w-9 items-center justify-center">
            <AccountButton />
          </div>
        ) : null}
      </div>
    </header>
  )
}
