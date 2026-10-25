import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { Banner, ButtonLink, ChevronLeft, Surface, Wordmark } from '../ui'
import { RouteErrorBoundary } from './RouteErrorBoundary'

/**
 * Frame for /admin, which lives outside the shell and the org gate: a slim bar
 * with the wordmark and a "Back to app" pill above the (separately restyled)
 * admin page. A crash in the admin page shows a message here instead of a
 * blank screen. (The page itself renders in a local sign-in-off run: it only
 * calls Clerk hooks when sign-in is enabled.)
 */
export function AdminFrame({ children }: { children: ReactNode }) {
  return (
    <Surface tone="light" className="min-h-screen">
      <header className="sticky top-0 z-30 flex h-14 items-center justify-between gap-3 border-b border-line bg-surface px-4 sm:px-6">
        <Link to="/app" aria-label="QuarkClinical home" className="flex items-center rounded-lg">
          <Wordmark />
        </Link>
        <ButtonLink as={Link} to="/app" variant="secondary" size="sm" leftIcon={<ChevronLeft />}>
          Back to app
        </ButtonLink>
      </header>
      <RouteErrorBoundary
        fallback={error => (
          <div className="mx-auto max-w-2xl px-4 py-10">
            <Banner tone="warning" title="The admin console could not load">
              Something went wrong while opening this page. Go back to the app and try again.
              <span className="mt-1 block text-fg-subtle">{error.message}</span>
            </Banner>
          </div>
        )}
      >
        {children}
      </RouteErrorBoundary>
    </Surface>
  )
}
