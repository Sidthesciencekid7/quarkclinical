import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { PillNav } from '../PillNav'
import { Search } from '../icons'

const items = [
  { id: 'protocol', label: 'Protocol', icon: <Search /> },
  { id: 'budget', label: 'Budget' },
]

describe('PillNav (toggle mode)', () => {
  it('sets aria-pressed on the active item only', () => {
    render(<PillNav ariaLabel="Mode" items={items} value="protocol" />)
    expect(screen.getByRole('group', { name: 'Mode' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Protocol' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('button', { name: 'Budget' })).toHaveAttribute('aria-pressed', 'false')
    expect(screen.getByRole('button', { name: 'Budget' })).not.toHaveAttribute('aria-current')
  })

  it('reports the chosen id', () => {
    const onChange = vi.fn()
    render(<PillNav ariaLabel="Mode" items={items} value="protocol" onChange={onChange} />)
    fireEvent.click(screen.getByRole('button', { name: 'Budget' }))
    expect(onChange).toHaveBeenCalledWith('budget')
  })

  it('does not fire onChange for disabled items', () => {
    const onChange = vi.fn()
    render(
      <PillNav
        ariaLabel="Mode"
        items={[
          { id: 'a', label: 'A' },
          { id: 'b', label: 'B', disabled: true },
        ]}
        value="a"
        onChange={onChange}
      />,
    )
    fireEvent.click(screen.getByRole('button', { name: 'B' }))
    expect(onChange).not.toHaveBeenCalled()
  })
})

describe('PillNav (nav mode)', () => {
  const navItems = [
    { id: 'products', label: 'Products', href: '#products' },
    { id: 'pricing', label: 'Pricing', href: '#pricing' },
  ]

  it('sets aria-current on the active link and none on the others', () => {
    render(<PillNav ariaLabel="Sections" mode="nav" items={navItems} value="pricing" />)
    expect(screen.getByRole('navigation', { name: 'Sections' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Pricing' })).toHaveAttribute('aria-current', 'page')
    expect(screen.getByRole('link', { name: 'Products' })).not.toHaveAttribute('aria-current')
    expect(screen.getByRole('link', { name: 'Pricing' })).not.toHaveAttribute('aria-pressed')
  })

  it('supports a custom aria-current token and a custom renderer', () => {
    render(
      <PillNav
        ariaLabel="Sections"
        mode="nav"
        current="true"
        items={navItems}
        value="products"
        renderItem={(item, p) => (
          <a data-custom href={item.href} className={p.className} aria-current={p['aria-current']}>
            {p.children}
          </a>
        )}
      />,
    )
    const link = screen.getByRole('link', { name: 'Products' })
    expect(link).toHaveAttribute('data-custom')
    expect(link).toHaveAttribute('aria-current', 'true')
    expect(link).toHaveClass('pillnav-item')
  })
})
