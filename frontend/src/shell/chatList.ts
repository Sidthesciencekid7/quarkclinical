/**
 * Recent chat sessions for the sidebar and Home.
 *
 * The backend chat routes do not exist until the chat wave ships, so GET
 * /api/chat answers 404. That is an expected state, not an error: it is
 * remembered for a few minutes (so the app does not retry, and log a failed
 * request, on every navigation) and callers simply get `available: false`.
 */
import { listChats } from '../api/chat'
import { HttpError } from '../api/client'
import type { ChatSessionListItem } from '../chat/types'

export interface RecentChats {
  items: ChatSessionListItem[]
  /** False when the chat API is missing (404/405) or the request failed. */
  available: boolean
}

const RETRY_AFTER_MISSING_MS = 5 * 60 * 1000
let missingUntil = 0
let inFlight: Promise<RecentChats> | null = null

/** Test hook: forget that the chat API was missing. */
export function resetChatListState(): void {
  missingUntil = 0
  inFlight = null
}

async function load(limit: number): Promise<RecentChats> {
  try {
    const items = await listChats({ limit })
    return { items: Array.isArray(items) ? items : [], available: true }
  } catch (e) {
    if (e instanceof HttpError && (e.status === 404 || e.status === 405)) {
      missingUntil = Date.now() + RETRY_AFTER_MISSING_MS
    }
    return { items: [], available: false }
  }
}

/** Concurrent callers (sidebar + Home, a remounted effect) share one request. */
export function fetchRecentChats(limit = 5): Promise<RecentChats> {
  if (Date.now() < missingUntil) return Promise.resolve({ items: [], available: false })
  if (!inFlight) {
    inFlight = load(limit).finally(() => {
      inFlight = null
    })
  }
  return inFlight
}

export const CHAT_KIND_LABELS: Record<string, string> = {
  protocol: 'Protocol',
  budget: 'Budget',
  protocol_edit: 'Protocol edit',
}
