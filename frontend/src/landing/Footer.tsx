import { Link } from 'react-router-dom'
import { Button, Surface } from '../ui'
import { Wordmark } from '../components/QuarkLogo'

const PRODUCT_LINKS: [string, string][] = [
  ['Products', '#products'],
  ['How it works', '#how'],
  ['Pricing', '#pricing'],
  ['FAQ', '#faq'],
]

const LINK_CLASS =
  'rounded-md text-small text-fg-muted transition-colors duration-fast hover:text-fg'

export function Footer({ onContact }: { onContact: () => void }) {
  return (
    <Surface as="footer" tone="dark">
      <div className="mx-auto grid max-w-6xl grid-cols-2 gap-8 px-5 py-14 md:grid-cols-4">
        <div className="col-span-2 md:col-span-1">
          <Wordmark tone="dark" size="md" showMark />
          <p className="mt-4 max-w-xs text-small text-fg-muted">
            Protocols, budgets and study reports for clinical pharmacology teams.
          </p>
        </div>
        <nav aria-label="Product">
          <p className="mb-4 text-eyebrow uppercase text-fg-subtle">Product</p>
          <ul className="m-0 flex list-none flex-col gap-2.5 p-0">
            {PRODUCT_LINKS.map(([label, href]) => (
              <li key={label}>
                <a href={href} className={LINK_CLASS}>
                  {label}
                </a>
              </li>
            ))}
          </ul>
        </nav>
        <div>
          <p className="mb-4 text-eyebrow uppercase text-fg-subtle">Company</p>
          <ul className="m-0 flex list-none flex-col gap-2.5 p-0">
            <li>
              <Button
                variant="link"
                size="sm"
                onClick={onContact}
                className="!text-fg-muted hover:!text-fg"
              >
                Book a demo
              </Button>
            </li>
          </ul>
        </div>
        <nav aria-label="Legal">
          <p className="mb-4 text-eyebrow uppercase text-fg-subtle">Legal</p>
          <ul className="m-0 flex list-none flex-col gap-2.5 p-0">
            <li>
              <Link to="/privacy" className={LINK_CLASS}>
                Privacy
              </Link>
            </li>
            <li>
              <Link to="/terms" className={LINK_CLASS}>
                Terms
              </Link>
            </li>
          </ul>
        </nav>
      </div>
      <div className="border-t border-line">
        <div className="mx-auto flex max-w-6xl flex-col items-center justify-between gap-3 px-5 py-6 text-small text-fg-subtle sm:flex-row">
          <p className="m-0">© {new Date().getFullYear()} QuarkClinical. For investigational use; outputs require expert review.</p>
          <p className="m-0">Audit trail · Role-based review · Electronic signature</p>
        </div>
      </div>
    </Surface>
  )
}
