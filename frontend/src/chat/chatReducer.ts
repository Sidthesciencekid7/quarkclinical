/**
 * Pure state machine behind `useChatSession`: no timers, no network, no React.
 * Every transition is an action, so a scripted list of server events can be
 * reduced and asserted in a plain unit test.
 *
 * Vocabulary
 *  - entries:  the finished transcript (server messages, optimistic user
 *              messages, local notices), oldest first.
 *  - segments: the assistant text of the turn that is streaming right now. One
 *              segment per model call (text that arrives after a tool ran starts
 *              a new one). Cards that arrive attach to the segment that issued
 *              the tool call, mirroring the server's one-message-per-call rows.
 *  - cards:    every card of the session, upserted by id; which message shows a
 *              card comes from `ChatMessageDisplay.card_ids`, or, for cards an
 *              action created, from `anchors`.
 */
import { deriveGuide } from './guide'
import { toPlainText } from './markdown'
import type {
  ActionResponse,
  Card,
  ChatMessageDisplay,
  ChatSessionDetail,
  ChatSessionRead,
  ChatStreamEvent,
  Guide,
  MessageFlags,
  NoticeLevel,
  SpecSummary,
} from './types'

export interface AssistantSegment {
  id: string
  text: string
  cardIds: string[]
  /** A tool ran since the last text: the next text starts a new segment. */
  sealed: boolean
}

export type ThreadEntry =
  | { kind: 'message'; message: ChatMessageDisplay }
  | { kind: 'notice'; id: string; level: NoticeLevel; text: string }

export interface TurnError {
  code: string
  message: string
  retryable: boolean
}

export type BannerKind = 'license' | 'rate' | 'busy' | 'info'

export interface ChatBanner {
  kind: BannerKind
  tone: 'info' | 'warning' | 'danger'
  title?: string
  text: string
}

export interface ChatState {
  load: 'idle' | 'loading' | 'ready' | 'error'
  loadError: string | null
  loadErrorKind: string | null
  session: ChatSessionRead | null
  entries: ThreadEntry[]
  cards: Card[]
  /** Cards no message references (created by an action): card id -> entry id to render after. */
  anchors: Record<string, string>
  spec: SpecSummary | null
  guide: Guide | null
  awaitingReply: boolean

  streaming: boolean
  turnId: string | null
  segments: AssistantSegment[]
  liveNotices: Array<{ id: string; level: NoticeLevel; text: string }>
  toolLabel: string | null
  turnError: TurnError | null
  banner: ChatBanner | null
  /** Text of the last finished assistant message, for the polite live region. */
  announcement: string
  /** The client stopped listening but the server may still be finishing a turn. */
  recovering: boolean
  /** Text a rejected send should put back in the composer. */
  rejectedText: string | null
  cardErrors: Record<string, string>
  pendingAction: { cardId: string; action: string } | null
}

export const initialChatState: ChatState = {
  load: 'idle',
  loadError: null,
  loadErrorKind: null,
  session: null,
  entries: [],
  cards: [],
  anchors: {},
  spec: null,
  guide: null,
  awaitingReply: false,
  streaming: false,
  turnId: null,
  segments: [],
  liveNotices: [],
  toolLabel: null,
  turnError: null,
  banner: null,
  announcement: '',
  recovering: false,
  rejectedText: null,
  cardErrors: {},
  pendingAction: null,
}

export type ChatAction =
  | { type: 'load_start' }
  | { type: 'load_error'; message: string; errorKind: string | null }
  | { type: 'loaded'; detail: ChatSessionDetail }
  | { type: 'turn_begin'; localId: string; userText: string | null; now?: string }
  | { type: 'stream_event'; event: ChatStreamEvent; now?: string }
  /** The stream stopped without a terminal event (user pressed Stop, or the page is leaving). */
  | { type: 'turn_finish'; now?: string }
  /** The turn could not start (HTTP error before the stream opened). */
  | { type: 'turn_rejected'; localId: string; userText: string | null; error: TurnError | null; banner: ChatBanner | null }
  | { type: 'action_begin'; cardId: string; action: string }
  | { type: 'action_done'; response: ActionResponse }
  | { type: 'action_failed'; cardId: string; message: string }
  | { type: 'set_banner'; banner: ChatBanner | null }
  | { type: 'clear_turn_error' }
  | { type: 'recovering'; value: boolean }
  | { type: 'clear_rejected_text' }
  | { type: 'drop_card_error'; cardId: string }

/* ---------------------------------------------------------------- helpers */

const LOCAL_PREFIX = 'local-'

export function isLocalId(id: string): boolean {
  return id.startsWith(LOCAL_PREFIX)
}

export function upsertCard(cards: Card[], card: Card): Card[] {
  const i = cards.findIndex(c => c.id === card.id)
  if (i === -1) return [...cards, card]
  const next = cards.slice()
  next[i] = card
  return next
}

function upsertCards(cards: Card[], incoming: Card[]): Card[] {
  return incoming.reduce(upsertCard, cards)
}

function nowIso(now?: string): string {
  return now ?? new Date().toISOString()
}

function lastSeq(entries: ThreadEntry[]): number {
  let seq = 0
  for (const e of entries) if (e.kind === 'message' && e.message.seq > seq) seq = e.message.seq
  return seq
}

function lastMessageId(entries: ThreadEntry[]): string | null {
  for (let i = entries.length - 1; i >= 0; i--) {
    const e = entries[i]
    if (e.kind === 'message') return e.message.id
  }
  return null
}

/** The anchor used for a card that arrives before any message exists. */
export const START_ANCHOR = '__start__'

function referencedCardIds(entries: ThreadEntry[]): Set<string> {
  const ids = new Set<string>()
  for (const e of entries) if (e.kind === 'message') for (const id of e.message.card_ids) ids.add(id)
  return ids
}

/** Pin every card no message shows (and that has no anchor yet) after the current last message,
 *  so a later turn's messages never jump above it. */
function anchorOrphans(state: ChatState): Record<string, string> {
  const referenced = referencedCardIds(state.entries)
  const live = new Set(state.segments.flatMap(s => s.cardIds))
  const anchor = lastMessageId(state.entries) ?? START_ANCHOR
  let anchors = state.anchors
  for (const c of state.cards) {
    if (referenced.has(c.id) || live.has(c.id) || anchors[c.id]) continue
    if (anchors === state.anchors) anchors = { ...anchors }
    anchors[c.id] = anchor
  }
  return anchors
}

export function bannerForCode(code: string, message: string): ChatBanner | null {
  switch (code) {
    case 'license_required':
      return {
        kind: 'license',
        tone: 'warning',
        title: 'License not active yet',
        text:
          "Your organization's license is not active yet, so the assistant is paused and nothing can be generated. What you have written is saved in this conversation, and you can carry on here once an administrator activates the license.",
      }
    case 'rate_limited':
      return { kind: 'rate', tone: 'warning', title: 'Slow down a little', text: message || 'You are sending messages too quickly. Wait a moment and try again.' }
    case 'daily_cap':
      return { kind: 'rate', tone: 'warning', title: 'Daily message limit reached', text: message || 'You have reached today’s limit of assistant messages. Card actions still work; messages resume tomorrow.' }
    case 'session_cap':
      return { kind: 'rate', tone: 'warning', title: 'This conversation is full', text: message || 'This conversation has reached its message limit. Start a new chat to continue.' }
    default:
      return null
  }
}

/** Map a ChatApiError-ish (kind, code, message) to the banner and/or inline error to show. */
export function describeFailure(kind: string, code: string | null, message: string): { banner: ChatBanner | null; error: TurnError | null } {
  if (kind === 'busy') {
    return {
      banner: { kind: 'busy', tone: 'info', text: 'Another reply is still running in this conversation. It will show up here when it finishes.' },
      error: null,
    }
  }
  if (kind === 'license') return { banner: bannerForCode('license_required', message), error: null }
  if (kind === 'rate_limit') {
    const banner = bannerForCode(code ?? 'rate_limited', message) ?? bannerForCode('rate_limited', message)
    return { banner, error: null }
  }
  if (kind === 'forbidden') return { banner: null, error: { code: 'forbidden', message: message || 'You do not have permission to do that.', retryable: false } }
  return { banner: null, error: { code: code ?? kind, message: message || 'Something went wrong. Please try again.', retryable: kind !== 'validation' } }
}

/* -------------------------------------------------------- streaming parts */

function finalizeSegments(state: ChatState, end: { messageId: string; flags: MessageFlags } | null, now: string): ChatState {
  const newEntries: ThreadEntry[] = []
  let seq = lastSeq(state.entries)
  const kept = state.segments.filter(s => s.text.trim() !== '' || s.cardIds.length > 0)
  let flagTarget = -1
  kept.forEach((s, i) => {
    if (s.text.trim() !== '') flagTarget = i
  })
  if (flagTarget === -1) flagTarget = kept.length - 1
  kept.forEach((s, i) => {
    seq += 1
    const isLast = i === kept.length - 1
    newEntries.push({
      kind: 'message',
      message: {
        id: end && isLast ? end.messageId : `${LOCAL_PREFIX}${state.turnId ?? 'turn'}-${i}`,
        seq,
        role: 'assistant',
        text: s.text,
        card_ids: s.cardIds,
        flags: end && i === flagTarget ? end.flags : { unverified_numbers: [] },
        created_at: now,
      },
    })
  })
  for (const n of state.liveNotices) newEntries.push({ kind: 'notice', id: n.id, level: n.level, text: n.text })

  const lastText = [...kept].reverse().find(s => s.text.trim() !== '')
  return {
    ...state,
    entries: [...state.entries, ...newEntries],
    streaming: false,
    turnId: null,
    segments: [],
    liveNotices: [],
    toolLabel: null,
    awaitingReply: false,
    announcement: lastText ? toPlainText(lastText.text) : state.announcement,
  }
}

function reduceStreamEvent(state: ChatState, event: ChatStreamEvent, now: string): ChatState {
  switch (event.event) {
    case 'turn_start':
      return { ...state, turnId: event.data.turn_id }

    case 'text_delta': {
      if (!event.data.text) return state
      const segments = state.segments.slice()
      let last = segments[segments.length - 1]
      if (!last || last.sealed) {
        last = { id: `${LOCAL_PREFIX}seg-${segments.length}`, text: '', cardIds: [], sealed: false }
        segments.push(last)
      } else {
        last = { ...last }
        segments[segments.length - 1] = last
      }
      last.text += event.data.text
      return { ...state, segments, toolLabel: null }
    }

    case 'tool_start': {
      const segments = state.segments.slice()
      if (segments.length === 0) {
        segments.push({ id: `${LOCAL_PREFIX}seg-0`, text: '', cardIds: [], sealed: true })
      } else {
        const last = segments[segments.length - 1]
        if (!last.sealed) segments[segments.length - 1] = { ...last, sealed: true }
      }
      return { ...state, segments, toolLabel: event.data.label || 'Working…' }
    }

    case 'card': {
      const card = event.data
      const exists = state.cards.some(c => c.id === card.id)
      const cards = upsertCard(state.cards, card)
      if (exists) return { ...state, cards }
      const segments = state.segments.slice()
      if (segments.length === 0) {
        segments.push({ id: `${LOCAL_PREFIX}seg-0`, text: '', cardIds: [card.id], sealed: true })
      } else {
        const last = segments[segments.length - 1]
        segments[segments.length - 1] = { ...last, cardIds: [...last.cardIds, card.id] }
      }
      return { ...state, cards, segments }
    }

    case 'spec':
      return {
        ...state,
        spec: event.data.summary,
        session: state.session ? { ...state.session, spec_version: event.data.spec_version } : state.session,
      }

    case 'notice':
      return { ...state, liveNotices: [...state.liveNotices, { id: `${LOCAL_PREFIX}notice-${state.liveNotices.length}-${state.entries.length}`, level: event.data.level, text: event.data.text }] }

    case 'turn_end': {
      const done = finalizeSegments(state, { messageId: event.data.message_id, flags: event.data.flags ?? { unverified_numbers: [] } }, now)
      const kind = done.session?.kind
      return { ...done, guide: kind ? deriveGuide(kind, done.spec) : done.guide, turnError: null }
    }

    case 'error': {
      const done = finalizeSegments(state, null, now)
      const code = event.data.code
      const banner = bannerForCode(code, event.data.message)
      return {
        ...done,
        banner: banner ?? done.banner,
        turnError: banner ? null : { code, message: event.data.message, retryable: event.data.retryable },
      }
    }
  }
}

/* ---------------------------------------------------------------- reducer */

function entriesFromMessages(messages: ChatMessageDisplay[]): ThreadEntry[] {
  return messages
    .filter(m => m.role !== 'tool_results' && m.role !== 'system_note')
    .map(message => ({ kind: 'message' as const, message }))
}

export function chatReducer(state: ChatState, action: ChatAction): ChatState {
  switch (action.type) {
    case 'load_start':
      return { ...state, load: state.load === 'ready' ? 'ready' : 'loading', loadError: null, loadErrorKind: null }

    case 'load_error':
      return { ...state, load: 'error', loadError: action.message, loadErrorKind: action.errorKind }

    case 'loaded': {
      const d = action.detail
      const entries = entriesFromMessages(d.messages ?? [])
      const ids = new Set(entries.flatMap(e => (e.kind === 'message' ? [e.message.id] : [])))
      const anchors: Record<string, string> = {}
      for (const [cardId, anchor] of Object.entries(state.anchors)) {
        if (anchor === START_ANCHOR || ids.has(anchor)) anchors[cardId] = anchor
      }
      return {
        ...state,
        load: 'ready',
        loadError: null,
        loadErrorKind: null,
        session: d.session,
        entries,
        cards: d.cards ?? [],
        anchors,
        spec: d.spec,
        awaitingReply: d.awaiting_reply,
        // A load while a stream is open must not wipe the streaming parts.
        guide: state.guide ?? deriveGuide(d.session.kind, d.spec),
      }
    }

    case 'turn_begin': {
      const anchors = anchorOrphans(state)
      const entries = action.userText
        ? [
            ...state.entries,
            {
              kind: 'message' as const,
              message: {
                id: `${LOCAL_PREFIX}user-${action.localId}`,
                seq: lastSeq(state.entries) + 1,
                role: 'user' as const,
                text: action.userText,
                card_ids: [],
                flags: { unverified_numbers: [] },
                created_at: nowIso(action.now),
              },
            },
          ]
        : state.entries
      return {
        ...state,
        anchors,
        entries,
        streaming: true,
        turnId: null,
        segments: [],
        liveNotices: [],
        toolLabel: null,
        turnError: null,
        banner: state.banner?.kind === 'busy' ? null : state.banner,
        guide: null,
        rejectedText: null,
        awaitingReply: false,
      }
    }

    case 'stream_event':
      return reduceStreamEvent(state, action.event, nowIso(action.now))

    case 'turn_finish':
      return state.streaming ? finalizeSegments(state, null, nowIso(action.now)) : state

    case 'turn_rejected': {
      const entries = state.entries.filter(e => !(e.kind === 'message' && e.message.id === `${LOCAL_PREFIX}user-${action.localId}`))
      return {
        ...state,
        entries,
        streaming: false,
        segments: [],
        liveNotices: [],
        toolLabel: null,
        turnError: action.error,
        banner: action.banner ?? state.banner,
        rejectedText: action.userText,
        guide: state.guide ?? (state.session ? deriveGuide(state.session.kind, state.spec) : null),
      }
    }

    case 'action_begin':
      return { ...state, pendingAction: { cardId: action.cardId, action: action.action }, cardErrors: dropKey(state.cardErrors, action.cardId) }

    case 'action_done': {
      const r = action.response
      const cards = upsertCards(state.cards, r.cards ?? [])
      return {
        ...state,
        pendingAction: null,
        cards,
        spec: r.spec,
        session: state.session ? { ...state.session, spec_version: r.spec_version } : state.session,
        guide: r.guide,
        banner: state.banner?.kind === 'license' || state.banner?.kind === 'busy' ? null : state.banner,
      }
    }

    case 'action_failed':
      return { ...state, pendingAction: null, cardErrors: { ...state.cardErrors, [action.cardId]: action.message } }

    case 'set_banner':
      return { ...state, banner: action.banner }

    case 'clear_turn_error':
      return { ...state, turnError: null }

    case 'recovering':
      return { ...state, recovering: action.value }

    case 'clear_rejected_text':
      return { ...state, rejectedText: null }

    case 'drop_card_error':
      return { ...state, cardErrors: dropKey(state.cardErrors, action.cardId) }
  }
}

function dropKey<T>(obj: Record<string, T>, key: string): Record<string, T> {
  if (!(key in obj)) return obj
  const { [key]: _gone, ...rest } = obj
  return rest
}

/* ------------------------------------------------------------ the thread */

export interface ThreadRow {
  key: string
  kind: 'user' | 'assistant' | 'note' | 'notice' | 'cards'
  text: string
  cards: Card[]
  flags: MessageFlags
  /** This row is the assistant text that is still streaming. */
  streaming: boolean
  level?: NoticeLevel
  messageId?: string
}

const NO_FLAGS: MessageFlags = { unverified_numbers: [] }

/** Flatten state into the rows the thread renders, oldest first. Pure. */
export function selectThread(state: ChatState): ThreadRow[] {
  const byId = new Map(state.cards.map(c => [c.id, c]))
  const referenced = referencedCardIds(state.entries)
  const liveIds = new Set(state.segments.flatMap(s => s.cardIds))

  const anchoredTo = new Map<string, Card[]>()
  const loose: Card[] = []
  for (const card of state.cards) {
    if (referenced.has(card.id) || liveIds.has(card.id)) continue
    const anchor = state.anchors[card.id]
    if (anchor) {
      const list = anchoredTo.get(anchor) ?? []
      list.push(card)
      anchoredTo.set(anchor, list)
    } else {
      loose.push(card)
    }
  }
  const knownAnchors = new Set<string>([START_ANCHOR])
  for (const e of state.entries) if (e.kind === 'message') knownAnchors.add(e.message.id)

  const rows: ThreadRow[] = []
  const startCards = anchoredTo.get(START_ANCHOR)
  if (startCards) rows.push({ key: 'cards-start', kind: 'cards', text: '', cards: startCards, flags: NO_FLAGS, streaming: false })

  for (const e of state.entries) {
    if (e.kind === 'notice') {
      rows.push({ key: e.id, kind: 'notice', text: e.text, cards: [], flags: NO_FLAGS, streaming: false, level: e.level })
      continue
    }
    const m = e.message
    const own = m.card_ids.map(id => byId.get(id)).filter((c): c is Card => !!c)
    const extra = anchoredTo.get(m.id) ?? []
    const cards = extra.length ? [...own, ...extra] : own
    rows.push({
      key: m.id,
      kind: m.role === 'user' ? 'user' : m.role === 'assistant' ? 'assistant' : 'note',
      text: m.text,
      cards,
      flags: m.flags ?? NO_FLAGS,
      streaming: false,
      messageId: m.id,
    })
  }

  // Cards anchored to a message that no longer exists, and brand-new action cards.
  const strays = [...loose]
  for (const [anchor, cards] of anchoredTo) if (!knownAnchors.has(anchor)) strays.push(...cards)
  if (strays.length) rows.push({ key: 'cards-latest', kind: 'cards', text: '', cards: strays, flags: NO_FLAGS, streaming: false })

  state.segments.forEach((s, i) => {
    const cards = s.cardIds.map(id => byId.get(id)).filter((c): c is Card => !!c)
    const isLast = i === state.segments.length - 1
    rows.push({ key: `${s.id}-live`, kind: 'assistant', text: s.text, cards, flags: NO_FLAGS, streaming: state.streaming && isLast && !s.sealed })
  })
  for (const n of state.liveNotices) {
    rows.push({ key: n.id, kind: 'notice', text: n.text, cards: [], flags: NO_FLAGS, streaming: false, level: n.level })
  }
  return rows
}
