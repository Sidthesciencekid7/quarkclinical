import { fireEvent, render, screen } from '@testing-library/react'
import { useState } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { Wordmark } from '../../components/QuarkLogo'
import { Badge } from '../Badge'
import { Banner } from '../Banner'
import { ChatCard } from '../Card'
import { Chip } from '../Chip'
import { Field, Input, Select } from '../Field'
import { Surface } from '../Surface'
import { TabPanel, Tabs } from '../Tabs'

describe('Chip', () => {
  it('is a plain suggestion chip without aria-pressed', () => {
    const onClick = vi.fn()
    render(<Chip onClick={onClick}>Adalimumab biosimilar</Chip>)
    const chip = screen.getByRole('button', { name: 'Adalimumab biosimilar' })
    expect(chip).not.toHaveAttribute('aria-pressed')
    fireEvent.click(chip)
    expect(onClick).toHaveBeenCalled()
  })

  it('is selectable with aria-pressed when `selected` is given', () => {
    const { rerender } = render(<Chip selected={false}>Healthy</Chip>)
    expect(screen.getByRole('button', { name: 'Healthy' })).toHaveAttribute('aria-pressed', 'false')
    rerender(<Chip selected>Healthy</Chip>)
    const chip = screen.getByRole('button', { name: 'Healthy' })
    expect(chip).toHaveAttribute('aria-pressed', 'true')
    expect(chip).toHaveClass('chip-selected')
  })
})

describe('Tabs', () => {
  function Harness() {
    const [v, setV] = useState('a')
    return (
      <>
        <Tabs
          id="t"
          ariaLabel="Views"
          value={v}
          onChange={setV}
          items={[
            { id: 'a', label: 'Explore' },
            { id: 'b', label: 'History' },
          ]}
        />
        <TabPanel tabsId="t" id="a" value={v}>
          Explore content
        </TabPanel>
        <TabPanel tabsId="t" id="b" value={v}>
          History content
        </TabPanel>
      </>
    )
  }

  it('wires tab/tabpanel aria and switches with arrow keys', () => {
    render(<Harness />)
    const tabA = screen.getByRole('tab', { name: 'Explore' })
    expect(tabA).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByRole('tabpanel')).toHaveAccessibleName('Explore')
    fireEvent.keyDown(screen.getByRole('tablist'), { key: 'ArrowRight' })
    expect(screen.getByRole('tab', { name: 'History' })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByText('History content')).toBeInTheDocument()
    expect(screen.queryByText('Explore content')).not.toBeInTheDocument()
  })
})

describe('Field', () => {
  it('links label, hint and error to the control', () => {
    const { rerender } = render(
      <Field label="Drug name" hint="INN, e.g. adalimumab">
        <Input />
      </Field>,
    )
    const input = screen.getByLabelText('Drug name')
    expect(input).toHaveAccessibleDescription('INN, e.g. adalimumab')
    expect(input).not.toHaveAttribute('aria-invalid')

    rerender(
      <Field label="Drug name" hint="INN, e.g. adalimumab" error="Required">
        <Input />
      </Field>,
    )
    const bad = screen.getByLabelText('Drug name')
    expect(bad).toHaveAttribute('aria-invalid', 'true')
    expect(bad).toHaveAccessibleDescription('Required')
    expect(screen.getByRole('alert')).toHaveTextContent('Required')
  })

  it('Select picks up the field wiring too', () => {
    render(
      <Field label="Currency">
        <Select defaultValue="usd">
          <option value="usd">USD</option>
        </Select>
      </Field>,
    )
    expect(screen.getByLabelText('Currency')).toHaveValue('usd')
  })
})

describe('ChatCard', () => {
  it('disables its actions when superseded', () => {
    render(
      <ChatCard title="Study design" superseded actions={<button type="button">Confirm</button>}>
        body
      </ChatCard>,
    )
    expect(screen.getByRole('button', { name: 'Confirm' })).toBeDisabled()
    expect(screen.getByRole('article')).toHaveClass('opacity-60')
  })

  it('keeps actions live when active', () => {
    render(<ChatCard title="Study design" actions={<button type="button">Confirm</button>} />)
    expect(screen.getByRole('button', { name: 'Confirm' })).toBeEnabled()
  })
})

describe('Banner / Badge / Surface / Wordmark', () => {
  it('Banner uses role=alert for danger and role=status for info', () => {
    const { rerender } = render(<Banner tone="info">FYI</Banner>)
    expect(screen.getByRole('status')).toHaveTextContent('FYI')
    rerender(
      <Banner tone="danger" title="Failed">
        Something broke
      </Banner>,
    )
    expect(screen.getByRole('alert')).toHaveTextContent('Failed')
  })

  it('Badge applies its variant class', () => {
    render(<Badge variant="solid">New</Badge>)
    expect(screen.getByText('New')).toHaveClass('badge', 'badge-solid')
  })

  it('Surface sets data-surface', () => {
    render(
      <Surface tone="dark" data-testid="s">
        x
      </Surface>,
    )
    expect(screen.getByTestId('s')).toHaveAttribute('data-surface', 'dark')
  })

  it('Wordmark reads as QuarkClinical in both tones', () => {
    const { rerender } = render(<Wordmark />)
    expect(screen.getByRole('img', { name: 'QuarkClinical' })).toBeInTheDocument()
    rerender(<Wordmark tone="dark" />)
    expect(screen.getByRole('img', { name: 'QuarkClinical' })).toBeInTheDocument()
    expect(screen.getByText('Clinical')).toHaveClass('text-accent-300')
  })
})
