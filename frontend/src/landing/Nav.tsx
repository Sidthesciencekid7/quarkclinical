import { useEffect, useRef, useState } from 'react'
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
 * Floating glass navigation (the NORMA header): wordmark pill on the left,
 * segmented icon nav in the centre, "Log in" + "Book a demo" on the right.
 * Below 768px the centre nav collapses into a round menu button that opens a
 * glass sheet. The nav item for the section in view carries aria-current.
 */
export function Nav({
  active,
  onLogin,
  onDemo,
}: {
  active: LandingSectionId | null
  onLogin: () => void
  onDemo: () => void
}) {
  // True once the page has scrolled past the dark hero, i.e. the floating
  // pills now sit over light sections (they switch to a navy glass, see
  // landing.css).
  const [overLight, setOverLight] = useState(false)
  const [menuOpen, setMenuOpen] = useState(false)
  const menuButtonRef = useRef<HTMLButtonElement>(null)
  const sheetRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const onScroll = () => {
      const hero = document.getElementById('top')
      setOverLight(hero ? hero.getBoundingClientRect().bottom < 140 : window.scrollY > 600)
    }
    onScroll()
    window.addEventListener('scroll', onScroll, { passive: true })
    window.addEventListener('resize', onScroll)
    return () => {
      window.removeEventListener('scroll', onScroll)
      window.removeEventListener('resize', onScroll)
    }
  }, [])

  // Sheet: Esc closes and returns focus to the button; a click outside closes.
  useEffect(() => {
    if (!menuOpen) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        setMenuOpen(false)
        menuButtonRef.current?.focus()
      }
    }
    const onPointer = (e: PointerEvent) => {
      const t = e.target as Node
      if (sheetRef.current?.contains(t) || menuButtonRef.current?.contains(t)) return
      setMenuOpen(false)
    }
    document.addEventListener('keydown', onKey)
    document.addEventListener('pointerdown', onPointer)
    return () => {
      document.removeEventListener('keydown', onKey)
      document.removeEventListener('pointerdown', onPointer)
    }
  }, [menuOpen])

  const current = active ? SECTION_TO_NAV[active] : ''

  return (
    <header
      data-surface="dark"
      data-over-light={overLight}
      className="lp-nav fixed inset-x-0 top-0 z-50 bg-transparent text-fg"
    >
      <div className="relative mx-auto flex max-w-6xl items-center justify-between gap-3 px-4 pt-3 sm:px-5 sm:pt-4 md:grid md:grid-cols-[1fr_auto_1fr]">
        <a
          href="#top"
          aria-label="QuarkClinical, back to top"
          className="lp-glass inline-flex h-10 items-center rounded-pill px-4 md:justify-self-start"
        >
          <Wordmark tone="dark" size="sm" showMark />
        </a>

        <PillNav
          mode="nav"
          current="true"
          ariaLabel="Main"
          items={NAV_ITEMS}
          value={current}
          className="hidden md:inline-flex"
        />

        <div className="flex items-center gap-2 md:justify-self-end">
          <Button
            variant="secondary"
            size="md"
            className="hidden h-10 md:inline-flex"
            onClick={onLogin}
          >
            Log in
          </Button>
          <Button
            variant="primary"
            size="md"
            className="hidden h-10 min-[375px]:inline-flex"
            onClick={onDemo}
          >
            Book a demo
          </Button>
          <IconButton
            ref={menuButtonRef}
            label={menuOpen ? 'Close menu' : 'Open menu'}
            icon={menuOpen ? <X /> : <MenuIcon />}
            variant="secondary"
            size="md"
            className="!h-10 !w-10 md:hidden"
            aria-expanded={menuOpen}
            aria-controls="lp-mobile-menu"
            onClick={() => setMenuOpen(o => !o)}
          />
        </div>

        {menuOpen ? (
          <div
            ref={sheetRef}
            id="lp-mobile-menu"
            className="lp-sheet absolute inset-x-4 top-full mt-2 rounded-[24px] p-3 sm:inset-x-5 md:hidden"
          >
            <nav aria-label="Mobile">
              <ul className="m-0 flex list-none flex-col gap-1 p-0">
                {NAV_ITEMS.map(item => (
                  <li key={item.id}>
                    <a
                      href={item.href}
                      aria-current={item.id === current ? 'true' : undefined}
                      onClick={() => setMenuOpen(false)}
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
                  setMenuOpen(false)
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
                  setMenuOpen(false)
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
