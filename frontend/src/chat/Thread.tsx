import type { ReactNode } from 'react'
import { Spinner } from '../ui'
import { MessageBubble } from './MessageBubble'
import { CardRenderer } from './cards'
import type { ChatSessionApi } from './useChatSession'

export interface ThreadProps {
  chat: ChatSessionApi
  /** Shown when there is nothing in the conversation yet. */
  empty?: ReactNode
}

/**
 * The conversation: messages in order, each assistant message followed by the
 * cards it produced. Cards are rendered from server JSON only. While a reply
 * streams, a status line shows what the assistant is doing ("Checking the
 * DailyMed label...") or, before any text, that it is thinking.
 */
export function Thread({ chat, empty }: ThreadProps) {
  const { thread, state } = chat
  const writing = state.streaming
  const lastLive = thread[thread.length - 1]
  const showStatus = writing && (state.toolLabel !== null || !lastLive || lastLive.kind !== 'assistant' || !lastLive.streaming)

  if (thread.length === 0 && !writing) {
    return <>{empty ?? null}</>
  }

  return (
    <div className="space-y-6" data-testid="thread">
      {thread.map(row => {
        const hasBubble = row.kind !== 'cards'
        return (
          <div key={row.key} data-row={row.kind}>
            {hasBubble ? <MessageBubble row={row} /> : null}
            {row.cards.length > 0 ? (
              <div className={hasBubble && (row.text || row.kind === 'user') ? 'mt-3 space-y-3' : 'space-y-3'}>
                {row.cards.map(card => (
                  <CardRenderer key={card.id} card={card} />
                ))}
              </div>
            ) : null}
          </div>
        )
      })}
      {showStatus ? (
        <p className="flex items-center gap-2 text-small text-fg-muted" data-testid="turn-status">
          <Spinner size={14} />
          <span>{state.toolLabel ?? 'Thinking…'}</span>
        </p>
      ) : null}
    </div>
  )
}
