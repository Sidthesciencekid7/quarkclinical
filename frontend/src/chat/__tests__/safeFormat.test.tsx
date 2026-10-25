import { render } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { MessageBubble } from '../MessageBubble'
import { FormattedText, parseBlocks, parseInline } from '../safeFormat'
import type { ThreadRow } from '../chatReducer'

describe('parseInline', () => {
  it('splits plain, bold and code', () => {
    expect(parseInline('Use **adalimumab** with `FDA-BE-2014` now')).toEqual([
      { type: 'text', text: 'Use ' },
      { type: 'bold', text: 'adalimumab' },
      { type: 'text', text: ' with ' },
      { type: 'code', text: 'FDA-BE-2014' },
      { type: 'text', text: ' now' },
    ])
  })

  it('leaves an unmatched marker as literal text (a half-streamed reply)', () => {
    expect(parseInline('half **bold')).toEqual([{ type: 'text', text: 'half **bold' }])
    expect(parseInline('a ` tick')).toEqual([{ type: 'text', text: 'a ` tick' }])
  })
})

describe('parseBlocks', () => {
  it('makes paragraphs, bullet lists and numbered lists', () => {
    const blocks = parseBlocks('Intro line\nstill the intro\n\n- one\n- two\n\n1. first\n2. second\n\nOutro')
    expect(blocks).toEqual([
      { type: 'p', text: 'Intro line\nstill the intro' },
      { type: 'ul', items: ['one', 'two'] },
      { type: 'ol', items: ['first', 'second'], start: 1 },
      { type: 'p', text: 'Outro' },
    ])
  })

  it('accepts * and bullet characters and joins an indented continuation', () => {
    expect(parseBlocks('* a\n* b\n  more of b')).toEqual([{ type: 'ul', items: ['a', 'b more of b'] }])
  })

  it('treats a heading line as a bold paragraph, never as markup', () => {
    expect(parseBlocks('## Next steps')).toEqual([{ type: 'heading', text: 'Next steps' }])
  })
})

describe('FormattedText', () => {
  it('renders lists and emphasis as real elements', () => {
    const { container } = render(<FormattedText text={'**Plan**\n\n- Pick the drug\n- Confirm the design'} />)
    expect(container.querySelectorAll('ul > li')).toHaveLength(2)
    expect(container.querySelector('strong')?.textContent).toBe('Plan')
  })

  it('renders an HTML injection attempt as inert text', () => {
    const attack = '<img src=x onerror=alert(1)>'
    const { container } = render(<FormattedText text={`Look: ${attack}\n\n- <script>alert(2)</script>\n\n**<b>bold</b>** \`<i>code</i>\``} />)
    expect(container.querySelector('img')).toBeNull()
    expect(container.querySelector('script')).toBeNull()
    expect(container.querySelector('b')).toBeNull()
    expect(container.querySelector('i')).toBeNull()
    expect(container.textContent).toContain(attack)
    expect(container.textContent).toContain('<script>alert(2)</script>')
  })

  it('does not turn markdown links or images into elements', () => {
    const { container } = render(<FormattedText text={'[click](javascript:alert(1)) ![x](http://evil/x.png)'} />)
    expect(container.querySelector('a')).toBeNull()
    expect(container.querySelector('img')).toBeNull()
  })
})

describe('MessageBubble', () => {
  const base: ThreadRow = { key: 'k', kind: 'assistant', text: '', cards: [], flags: { unverified_numbers: [] }, streaming: false }

  it('shows the amber check-the-cards note only when numbers were flagged', () => {
    const plain = render(<MessageBubble row={{ ...base, text: 'All good.' }} />)
    expect(plain.queryByTestId('unverified-note')).toBeNull()
    plain.unmount()
    const flagged = render(<MessageBubble row={{ ...base, text: 'About 24 subjects.', flags: { unverified_numbers: ['24'] } }} />)
    expect(flagged.getByTestId('unverified-note')).toHaveTextContent('Check the cards for authoritative values.')
  })

  it('renders a user message as plain text, never as HTML', () => {
    const { container } = render(<MessageBubble row={{ ...base, kind: 'user', text: '<b>hi</b>' }} />)
    expect(container.querySelector('b')).toBeNull()
    expect(container.textContent).toBe('<b>hi</b>')
  })
})
