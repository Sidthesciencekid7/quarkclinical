/**
 * The pure half of the assistant-text formatter (no React): split text into
 * blocks and inline tokens, and flatten it to plain text for screen readers.
 * See safeFormat.tsx for the renderer and why nothing here can inject HTML.
 */

export type InlineToken = { type: 'text' | 'bold' | 'code'; text: string }

export type Block =
  | { type: 'p'; text: string }
  | { type: 'heading'; text: string }
  | { type: 'ul'; items: string[] }
  | { type: 'ol'; items: string[]; start: number }

const INLINE_RE = /(`[^`\n]+`|\*\*[^*\n]+?\*\*)/g

/** Split a line of text into plain / bold / code tokens. An unmatched `**` or
 *  backtick stays as literal text (a half-streamed reply never breaks). */
export function parseInline(src: string): InlineToken[] {
  const tokens: InlineToken[] = []
  let last = 0
  for (const m of src.matchAll(INLINE_RE)) {
    const index = m.index ?? 0
    if (index > last) tokens.push({ type: 'text', text: src.slice(last, index) })
    const raw = m[0]
    if (raw.startsWith('`')) tokens.push({ type: 'code', text: raw.slice(1, -1) })
    else tokens.push({ type: 'bold', text: raw.slice(2, -2) })
    last = index + raw.length
  }
  if (last < src.length) tokens.push({ type: 'text', text: src.slice(last) })
  return tokens
}

const BULLET_RE = /^\s{0,3}(?:[-*•])\s+(.*)$/
const ORDERED_RE = /^\s{0,3}(\d{1,3})[.)]\s+(.*)$/
const HEADING_RE = /^\s{0,3}#{1,6}\s+(.*)$/

/** Split text into blocks. Blank lines end a block; consecutive list lines form one list. */
export function parseBlocks(text: string): Block[] {
  const blocks: Block[] = []
  const lines = text.replace(/\r\n?/g, '\n').split('\n')
  let para: string[] = []
  let list: { type: 'ul' | 'ol'; items: string[]; start: number } | null = null

  const flushPara = () => {
    if (para.length) blocks.push({ type: 'p', text: para.join('\n') })
    para = []
  }
  const flushList = () => {
    if (list) blocks.push(list.type === 'ul' ? { type: 'ul', items: list.items } : { type: 'ol', items: list.items, start: list.start })
    list = null
  }

  for (const line of lines) {
    if (line.trim() === '') {
      flushPara()
      flushList()
      continue
    }
    const heading = HEADING_RE.exec(line)
    if (heading) {
      flushPara()
      flushList()
      blocks.push({ type: 'heading', text: heading[1].trim() })
      continue
    }
    const bullet = BULLET_RE.exec(line)
    const ordered = bullet ? null : ORDERED_RE.exec(line)
    if (bullet || ordered) {
      flushPara()
      const type = bullet ? 'ul' : 'ol'
      if (list && list.type !== type) flushList()
      if (!list) list = { type, items: [], start: ordered ? Number(ordered[1]) : 1 }
      list.items.push((bullet ? bullet[1] : ordered![2]).trim())
      continue
    }
    if (list && /^\s{2,}\S/.test(line)) {
      // An indented continuation of the previous list item.
      list.items[list.items.length - 1] += ` ${line.trim()}`
      continue
    }
    flushList()
    para.push(line.trim())
  }
  flushPara()
  flushList()
  return blocks
}

/** The text without markdown markers, for the polite live region (a screen
 *  reader should say "adalimumab", not "star star adalimumab star star"). */
export function toPlainText(text: string): string {
  return parseBlocks(text)
    .map(b => {
      if (b.type === 'ul') return b.items.map(i => parseInline(i).map(t => t.text).join('')).join('. ')
      if (b.type === 'ol') return b.items.map(i => parseInline(i).map(t => t.text).join('')).join('. ')
      return parseInline(b.text).map(t => t.text).join('').replace(/\s*\n\s*/g, ' ')
    })
    .join('\n')
}
