import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { ButtonLink, Surface } from '../ui'
import { Wordmark } from '../components/QuarkLogo'

/** Shared shell for the public legal pages (Terms, Privacy): a light header
 *  with the wordmark and a "Back to home" pill, a readable prose column and a
 *  small footer cross-linking the two documents. */
export default function LegalLayout({
  title,
  updated,
  children,
}: {
  title: string
  updated: string
  children: ReactNode
}) {
  return (
    <Surface tone="light" className="min-h-screen bg-surface-canvas">
      <header className="border-b border-line bg-surface">
        <div className="mx-auto flex h-16 max-w-3xl items-center justify-between px-5">
          <Link to="/" aria-label="QuarkClinical home" className="rounded-md">
            <Wordmark tone="light" size="md" showMark />
          </Link>
          <ButtonLink as={Link} to="/" variant="secondary" size="md">
            Back to home
          </ButtonLink>
        </div>
      </header>

      <main id="main" className="mx-auto max-w-3xl px-5 py-14 sm:py-16">
        <h1 className="mb-2 text-[2rem] font-semibold leading-tight tracking-[-0.025em] text-fg sm:text-[2.5rem]">
          {title}
        </h1>
        <p className="mb-10 text-small text-fg-subtle">Last updated {updated}</p>
        <div className="prose prose-zinc max-w-none prose-headings:font-semibold prose-headings:tracking-[-0.01em] prose-a:text-accent-700 prose-a:underline-offset-2 prose-strong:text-fg">
          {children}
        </div>
      </main>

      <footer className="border-t border-line bg-surface">
        <div className="mx-auto flex max-w-3xl flex-wrap items-center justify-between gap-3 px-5 py-6 text-small text-fg-subtle">
          <p className="m-0">© {new Date().getFullYear()} QuarkClinical</p>
          <nav aria-label="Legal" className="flex items-center gap-4">
            <Link to="/terms" className="rounded-md text-fg-muted hover:text-fg">
              Terms
            </Link>
            <Link to="/privacy" className="rounded-md text-fg-muted hover:text-fg">
              Privacy
            </Link>
          </nav>
        </div>
      </footer>
    </Surface>
  )
}
