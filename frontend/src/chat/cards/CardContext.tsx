import { createContext, useContext } from 'react'
import type { ActionPayloadMap, ActionResponse, ActionType, Card, SessionKind } from '../types'

/**
 * What a card needs from the conversation that renders it. The page (or the
 * edit drawer) provides one; cards never talk to the network themselves except
 * for the two read-only polls (generation progress, edit proposal) and the
 * drug search.
 */
export interface CardContextValue {
  sessionId: string
  kind: SessionKind
  /** Card buttons and inline controls are inert: a reply is streaming, or the client is waiting for the server to finish one. */
  busy: boolean
  /** The action currently in flight (one at a time). */
  pending: { cardId: string; action: string } | null
  /** Inline error per card id (the server's own wording). */
  errors: Record<string, string>
  /** Every card of the session (cards look each other up, e.g. "is there already a protocol_ready card?"). */
  cards: Card[]
  /** Submit a card action. Resolves with the response, or null when it failed (the error is in `errors`). */
  dispatch: <A extends ActionType>(card: Card, action: A, payload: ActionPayloadMap[A]) => Promise<ActionResponse | null>
  /** Send a chat message on the user's behalf (suggestion buttons inside cards). */
  sendText: (text: string) => void
  /** Refetch the conversation from the server. */
  resync: () => Promise<void>
}

const INERT: CardContextValue = {
  sessionId: '',
  kind: 'protocol',
  busy: true,
  pending: null,
  errors: {},
  cards: [],
  dispatch: async () => null,
  sendText: () => {},
  resync: async () => {},
}

export const CardContext = createContext<CardContextValue>(INERT)

/** The enclosing conversation. Outside a provider every control is inert rather than crashing. */
export function useCardContext(): CardContextValue {
  return useContext(CardContext)
}
