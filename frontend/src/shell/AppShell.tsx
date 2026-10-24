import { Suspense, useEffect, useRef } from 'react'
import { Outlet, useLocation } from 'react-router-dom'
import { Spinner, Surface, cx } from '../ui'
import { BreadcrumbProvider, useBreadcrumbs } from '../lib/breadcrumbs'
import { MobileDrawer } from './MobileDrawer'
import { RouteErrorBoundary } from './RouteErrorBoundary'
import { ShellProvider, useShell } from './ShellContext'
import { Sidebar } from './Sidebar'
import { TopBar } from './TopBar'

function ContentLoading() {
  return (
    <div className="flex min-h-[40vh] items-center justify-center gap-2 text-body text-fg-muted" role="status" aria-live="polite">
      <Spinner size={18} />
      <span>Loading…</span>
    </div>
  )
}

function ShellToast() {
  const { toast } = useShell()
  // The live region is always mounted so assistive tech announces the text when it appears.
  return (
    <div role="status" aria-live="polite" className="pointer-events-none fixed inset-x-0 bottom-6 z-[90] flex justify-center px-4">
      {toast ? (
        <div className="pointer-events-auto rounded-pill bg-ink-950 px-4 py-2 text-small font-medium text-white shadow-pop animate-ui-pop-in">
          {toast}
        </div>
      ) : null}
    </div>
  )
}

function ShellFrame() {
  const { rail, isMd, drawerOpen, closeDrawer, drawerOpenerRef } = useShell()
  const { pathname } = useLocation()
  const crumbs = useBreadcrumbs()
  const mainRef = useRef<HTMLElement>(null)

  // Tab title follows the current page.
  const pageTitle = crumbs.length ? crumbs[crumbs.length - 1].label : null
  useEffect(() => {
    const previous = document.title
    document.title = pageTitle ? `${pageTitle} · QuarkClinical` : 'QuarkClinical'
    return () => {
      document.title = previous
    }
  }, [pageTitle])

  // After a client-side navigation, move focus to the page area so keyboard and
  // screen-reader users start at the new content (not on the link they used).
  const firstRender = useRef(true)
  useEffect(() => {
    if (firstRender.current) {
      firstRender.current = false
      return
    }
    const t = window.setTimeout(() => mainRef.current?.focus({ preventScroll: true }), 0)
    return () => window.clearTimeout(t)
  }, [pathname])

  return (
    <Surface tone="light" className="flex h-dvh w-full overflow-hidden">
      <a
        href="#main-content"
        onClick={e => {
          e.preventDefault()
          mainRef.current?.focus()
        }}
        className="sr-only focus:not-sr-only focus:fixed focus:left-4 focus:top-3 focus:z-[100] focus:rounded-pill focus:bg-ink-950 focus:px-4 focus:py-2 focus:text-small focus:font-medium focus:text-white"
      >
        Skip to main content
      </a>

      {isMd ? (
        <aside
          id="app-sidebar"
          aria-label="Sidebar"
          data-rail={rail}
          className={cx(
            'flex-none overflow-hidden border-r border-line bg-surface transition-[width] duration-panel ease-out-soft',
            rail ? 'w-16' : 'w-[264px]',
          )}
        >
          <Sidebar rail={rail} />
        </aside>
      ) : null}

      <MobileDrawer open={drawerOpen} onClose={closeDrawer} label="Navigation" returnFocusTo={() => drawerOpenerRef.current}>
        <Sidebar rail={false} onClose={closeDrawer} onNavigate={closeDrawer} />
      </MobileDrawer>

      <div className="flex min-w-0 flex-1 flex-col">
        <TopBar />
        <main
          id="main-content"
          ref={mainRef}
          tabIndex={-1}
          className="min-h-0 flex-1 overflow-y-auto bg-surface outline-none focus:outline-none"
        >
          <RouteErrorBoundary resetKey={pathname}>
            <Suspense fallback={<ContentLoading />}>
              <Outlet />
            </Suspense>
          </RouteErrorBoundary>
        </main>
      </div>

      <ShellToast />
    </Surface>
  )
}

/**
 * The signed-in app's frame: sidebar (264px, or a 64px icon rail), top bar with
 * breadcrumbs, and the routed page. Mounted by App.tsx inside the Protected /
 * ActingUserProvider / OrgProvider / OrgGate wrappers, so identity and org are
 * already resolved when it renders.
 */
export default function AppShell() {
  return (
    <BreadcrumbProvider>
      <ShellProvider>
        <ShellFrame />
      </ShellProvider>
    </BreadcrumbProvider>
  )
}
