import { useEffect, useRef } from 'react'
import { Button, IconButton, PillNav, type PillNavItem } from '../ui'
import { Wordmark } from '../components/QuarkLogo'
import { Boxes, CircleDollarSign, CircleHelp, MenuIcon, Route, X } from './icons'
import { SECTION_TO_NAV, type LandingSectionId } from './handoff'

const NAV_ITEMS: PillNavItem[] = [
  { id: 'products', label: 'Products', icon: <Boxes aria-hidden />, href: '#products' },
  { id: 'how', label: 'How it works', icon: <Route aria-hidden />, href: '#how' },
  { id: 'pricing', label: 'Pricing', icon: <CircleDollarSign aria-hidden />, href: '#pricing' },
  { id: 'faq', label: 'FAQ', icon: <CircleHelp aria-hidden />, href: '#faq' },
]

/**
 * Compact floating glass navigation: wordmark pill on the left, segmented icon
 * nav in the centre, "Log in" + "Book a demo" on the right. The header itself
 * ignores pointer events so the page stays clickable around the pills. Below
 * 768px the centre nav collapses into a round menu button that opens a glass
 * sheet. The nav item for the section in view carries aria-current.
 *
 * The menu state lives in the page (`menuOpen`) so it can pause smooth
 * scrolling while the sheet is open.
 */
export function Nav({
  active,
  menuOpen,
  onMenuOpenChange,
  onLogin,
  onDemo,
}: {
  active: LandingSectionId | null
  menuOpen: boolean
  onMenuOpenChange: (open: boolean) => void
  onLogin: () => void
  onDemo: () => void
}) {
  const menuButtonRef = useRef<HTMLButtonElement>(null)
  const sheetRef = useRef<HTMLDivElement>(null)

  // Sheet: Esc closes and returns focus to the button; a click outside closes.
  useEffect(() => {
    if (!menuOpen) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        onMenuOpenChange(false)
        menuButtonRef.current?.focus()
      }
    }
    const onPointer = (e: PointerEvent) => {
      const t = e.target as Node
      if (sheetRef.current?.contains(t) || menuButtonRef.current?.contains(t)) return
      onMenuOpenChange(false)
    }
    document.addEventListener('keydown', onKey)
    document.addEventListener('pointerdown', onPointer)
    return () => {
      document.removeEventListener('keydown', onKey)
      document.removeEventListener('pointerdown', onPointer)
    }
  }, [menuOpen, onMenuOpenChange])

  const current = active ? SECTION_TO_NAV[active] : ''

  return (
    <header
      data-surface="dark"
      className="lp-nav pointer-events-none fixed inset-x-0 top-0 z-50 bg-transparent text-fg"
    >
      <div
        data-lp="navin"
        className="relative mx-auto flex max-w-[62rem] items-center justify-between gap-2 px-3 pt-3 sm:px-5 md:grid md:grid-cols-[1fr_auto_1fr]"
      >
        <a
          href="#top"
          aria-label="QuarkClinical, back to top"
          className="lp-glass pointer-events-auto inline-flex h-9 items-center rounded-pill px-3.5 md:justify-self-start"
        >
          <Wordmark tone="dark" size="sm" showMark />
        </a>

        <PillNav
          mode="nav"
          current="true"
          ariaLabel="Main"
          size="sm"
          items={NAV_ITEMS}
          value={current}
          className="pointer-events-auto hidden md:inline-flex"
        />

        <div className="pointer-events-auto flex items-center gap-2 md:justify-self-end">
          <Button variant="secondary" size="sm" className="hidden h-9 md:inline-flex" onClick={onLogin}>
            Log in
          </Button>
          <Button variant="primary" size="sm" className="hidden h-9 min-[375px]:inline-flex" onClick={onDemo}>
            Book a demo
          </Button>
          <IconButton
            ref={menuButtonRef}
            label={menuOpen ? 'Close menu' : 'Open menu'}
            icon={menuOpen ? <X /> : <MenuIcon />}
            variant="secondary"
            size="sm"
            className="!h-9 !w-9 md:hidden"
            aria-expanded={menuOpen}
            aria-controls="lp-mobile-menu"
            onClick={() => onMenuOpenChange(!menuOpen)}
          />
        </div>

        {menuOpen ? (
          <div
            ref={sheetRef}
            id="lp-mobile-menu"
            className="lp-sheet pointer-events-auto absolute inset-x-3 top-full mt-2 rounded-[24px] p-3 sm:inset-x-5 md:hidden"
          >
            <nav aria-label="Mobile">
              <ul className="m-0 flex list-none flex-col gap-1 p-0">
                {NAV_ITEMS.map(item => (
                  <li key={item.id}>
                    <a
                      href={item.href}
                      aria-current={item.id === current ? 'true' : undefined}
                      onClick={() => onMenuOpenChange(false)}
                      className="flex h-12 items-center gap-3 rounded-2xl px-4 text-[15px] font-medium text-fg-muted transition-colors duration-fast hover:bg-surface-hover hover:text-fg aria-[current=true]:bg-surface-hover aria-[current=true]:text-fg [&>svg]:h-4 [&>svg]:w-4"
                    >
                      {item.icon}
                      {item.label}
                    </a>
                  </li>
                ))}
              </ul>
            </nav>
            <div className="mt-2 flex flex-col gap-2 border-t border-line pt-3">
              <Button
                variant="secondary"
                size="lg"
                fullWidth
                onClick={() => {
                  onMenuOpenChange(false)
                  onLogin()
                }}
              >
                Log in
              </Button>
              <Button
                variant="primary"
                size="lg"
                fullWidth
                onClick={() => {
                  onMenuOpenChange(false)
                  onDemo()
                }}
              >
                Book a demo
              </Button>
            </div>
          </div>
        ) : null}
      </div>
    </header>
  )
}
