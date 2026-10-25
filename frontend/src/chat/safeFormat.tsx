/**
 * A deliberately tiny formatter for assistant text: paragraphs, bullet and
 * numbered lists, **bold** and `inline code`. Nothing else is interpreted.
 *
 * It parses to a small AST and renders React elements, so every character of
 * the model's text reaches the DOM as a text node (React escapes it).
 * `dangerouslySetInnerHTML` is never used: "<img src=x onerror=alert(1)>" is
 * shown as that literal text. Links, images and HTML are not supported on
 * purpose.
 */
import type { ReactNode } from 'react'

import { parseBlocks, parseInline } from './markdown'
export { parseBlocks, parseInline, toPlainText } from './markdown'
export type { Block, InlineToken } from './markdown'

function renderInline(src: string): ReactNode[] {
  return parseInline(src).map((t, i) => {
    if (t.type === 'bold') return <strong key={i} className="font-semibold">{t.text}</strong>
    if (t.type === 'code') {
      return (
        <code key={i} className="rounded-md bg-surface-muted px-1.5 py-0.5 font-mono text-[0.85em] text-fg">
          {t.text}
        </code>
      )
    }
    return t.text
  })
}

export interface FormattedTextProps {
  text: string
  className?: string
}

/** Assistant text as paragraphs / lists. Safe by construction (see the file header). */
export function FormattedText({ text, className }: FormattedTextProps) {
  const blocks = parseBlocks(text)
  return (
    <div className={className}>
      {blocks.map((b, i) => {
        const spacing = i === 0 ? '' : 'mt-3'
        if (b.type === 'ul') {
          return (
            <ul key={i} className={`${spacing} list-disc space-y-1 pl-5`}>
              {b.items.map((item, j) => (
                <li key={j}>{renderInline(item)}</li>
              ))}
            </ul>
          )
        }
        if (b.type === 'ol') {
          return (
            <ol key={i} start={b.start} className={`${spacing} list-decimal space-y-1 pl-5`}>
              {b.items.map((item, j) => (
                <li key={j}>{renderInline(item)}</li>
              ))}
            </ol>
          )
        }
        if (b.type === 'heading') {
          return (
            <p key={i} className={`${spacing} font-semibold`}>
              {renderInline(b.text)}
            </p>
          )
        }
        return (
          <p key={i} className={`${spacing} whitespace-pre-line break-words`}>
            {renderInline(b.text)}
          </p>
        )
      })}
    </div>
  )
}
