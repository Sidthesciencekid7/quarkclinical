import { forwardRef, useCallback, useEffect, useImperativeHandle, useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { Banner, Button, ButtonLink, Chip, Composer, EmptyState, MessageSquare, PanelLeft, Modal, Sparkles, Spinner, cx } from '../ui'
import { useMediaQuery } from '../shell/useMediaQuery'
import { SpecPanel } from './SpecPanel'
import { Thread } from './Thread'
import { CardContext } from './cards'
import type { CardContextValue } from './cards'
import { KIND_EMPTY_BODY, KIND_EMPTY_TITLES, KIND_LABELS, KIND_PLACEHOLDERS, STARTER_CHIPS } from './starters'
import type { ChatSessionApi } from './useChatSession'
import type { SessionKind } from './types'

export const XL_QUERY = '(min-width: 1280px)'

export interface ChatViewHandle {
  focusComposer: () => void
}

export interface ChatViewProps {
  chat: ChatSessionApi
  /** Fallback while the session has not loaded (the route already knows it). */
  kind?: SessionKind
  /** `page`: shell content area with the side panel at >= 1280px. `drawer`: fills its container. */
  variant?: 'page' | 'drawer'
  autoFocus?: boolean
  /** Shown on a failed load, e.g. a link back to Home. */
  onLeave?: ReactNode
}

/**
 * The conversation surface shared by the chat page and the edit drawer: the
 * scrolling thread, banners, and the docked composer with the guide line and
 * suggestion chips above it (an ElevenLabs-style floating composer).
 */
export const ChatView = forwardRef<ChatViewHandle, ChatViewProps>(function ChatView({ chat, kind: kindProp, variant = 'page', autoFocus, onLeave }, handleRef) {
  const { state } = chat
  const kind: SessionKind = state.session?.kind ?? kindProp ?? 'protocol'
  const isXl = useMediaQuery(XL_QUERY, false)
  const [sheetOpen, setSheetOpen] = useState(false)
  const composerRef = useRef<HTMLTextAreaElement | null>(null)
  const scrollerRef = useRef<HTMLDivElement | null>(null)
  const stickRef = useRef(true)
  const [draft, setDraft] = useState('')

  useImperativeHandle(handleRef, () => ({ focusComposer: () => composerRef.current?.focus() }), [])

  /* -------------------------------------------------------------- card context */
  const { dispatchAction, send, resync } = chat
  const cardContext = useMemo<CardContextValue>(
    () => ({
      sessionId: chat.sessionId,
      kind,
      busy: chat.busy || state.load !== 'ready' || state.pendingAction !== null,
      pending: state.pendingAction,
      errors: state.cardErrors,
      cards: state.cards,
      dispatch: (card, action, payload) => dispatchAction(card.id, action, payload),
      sendText: text => void send(text),
      resync,
    }),
    [chat.sessionId, kind, chat.busy, state.load, state.pendingAction, state.cardErrors, state.cards, dispatchAction, send, resync],
  )

  /* ------------------------------------------------------------ scroll behaviour */
  const onScroll = useCallback(() => {
    const el = scrollerRef.current
    if (!el) return
    stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 140
  }, [])

  const scrollSignal = `${chat.thread.length}:${chat.thread[chat.thread.length - 1]?.text.length ?? 0}:${chat.thread.reduce((n, r) => n + r.cards.length, 0)}:${state.toolLabel ?? ''}`
  useLayoutEffect(() => {
    const el = scrollerRef.current
    if (el && stickRef.current) el.scrollTop = el.scrollHeight
  }, [scrollSignal, state.streaming])

  // Opening a conversation starts at the bottom.
  useEffect(() => {
    if (state.load === 'ready') {
      stickRef.current = true
      const el = scrollerRef.current
      if (el) el.scrollTop = el.scrollHeight
    }
  }, [state.load])

  /* ---------------------------------------------------------------- composer */
  // A rejected send (busy / license / limit) puts the text back so nothing typed is lost.
  const { rejectedText, awaitingReply } = state
  const { clearRejectedText } = chat
  useEffect(() => {
    if (!rejectedText) return
    setDraft(prev => (prev.trim() ? prev : rejectedText))
    clearRejectedText()
  }, [rejectedText, clearRejectedText])

  // Focus returns to the composer when a reply finishes (if the user has not moved on to another control).
  const wasBusy = useRef(false)
  useEffect(() => {
    if (wasBusy.current && !chat.busy) {
      const active = document.activeElement
      if (!active || active === document.body || active.closest('[data-composer-dock]')) composerRef.current?.focus()
    }
    wasBusy.current = chat.busy
  }, [chat.busy])

  useEffect(() => {
    if (autoFocus && state.load === 'ready') composerRef.current?.focus()
  }, [autoFocus, state.load])

  const submit = useCallback(
    (text: string) => {
      setDraft('')
      void send(text)
      requestAnimationFrame(() => composerRef.current?.focus())
    },
    [send],
  )

  const empty = chat.thread.length === 0 && !state.streaming
  const guide = state.guide
  const chips = guide && guide.chips.length > 0 ? guide.chips : empty && state.load === 'ready' ? STARTER_CHIPS[kind] : []
  const showChips = !chat.busy && chips.length > 0
  const guideText = !chat.busy && !empty ? guide?.text : ''

  /* ----------------------------------------------------------------- failures */
  if (state.load === 'error') {
    const missing = state.loadErrorKind === 'not_found' || state.loadErrorKind === 'forbidden'
    return (
      <div className="mx-auto w-full max-w-xl px-4 py-12">
        <Banner tone="danger" title={missing ? 'This conversation is not available' : 'Could not open this conversation'}>
          {missing ? 'It may have been removed, or it belongs to someone else.' : state.loadError}
        </Banner>
        <div className="mt-4 flex gap-2">
          {missing ? null : (
            <Button variant="secondary" onClick={() => void chat.resync()}>
              Try again
            </Button>
          )}
          {onLeave}
        </div>
      </div>
    )
  }

  const banner = state.banner
  const turnError = state.turnError

  return (
    <CardContext.Provider value={cardContext}>
      <div className="flex h-full min-h-0 w-full">
        <div className="flex min-w-0 flex-1 flex-col">
          {/* Finished assistant messages are announced politely; streaming text is not. */}
          <div aria-live="polite" aria-atomic="true" className="sr-only" data-testid="announcer">
            {state.announcement}
          </div>

          <section aria-label="Conversation" ref={scrollerRef} onScroll={onScroll} className="min-h-0 flex-1 overflow-y-auto">
            <div className={cx('mx-auto w-full max-w-3xl px-4 pb-8 pt-6', variant === 'page' ? 'sm:px-6' : '')}>
              {state.load !== 'ready' ? (
                <div className="flex items-center justify-center gap-2 py-20 text-body text-fg-muted" role="status">
                  <Spinner size={18} />
                  <span>Opening conversation&hellip;</span>
                </div>
              ) : (
                <Thread
                  chat={chat}
                  empty={
                    <EmptyState
                      icon={<Sparkles />}
                      title={KIND_EMPTY_TITLES[kind]}
                      description={KIND_EMPTY_BODY[kind]}
                      className="py-16"
                    />
                  }
                />
              )}
            </div>
          </section>

          {/* The thread fades out behind the floating composer. */}
          <div aria-hidden className="pointer-events-none relative z-10 -mt-8 h-8 flex-none" style={{ background: 'linear-gradient(to top, var(--bg), transparent)' }} />

          <div data-composer-dock className="relative z-10 flex-none bg-surface px-4 pb-[max(1rem,env(safe-area-inset-bottom))]">
            <div className="mx-auto w-full max-w-3xl space-y-2.5">
              {banner ? (
                <Banner tone={banner.tone} title={banner.title} onDismiss={chat.dismissBanner}>
                  {banner.text}
                </Banner>
              ) : null}
              {turnError ? (
                <Banner
                  tone={turnError.retryable ? 'warning' : 'danger'}
                  title="That did not go through"
                  onDismiss={chat.dismissTurnError}
                  action={
                    turnError.retryable ? (
                      <Button size="sm" variant="secondary" disabled={chat.busy} onClick={() => void chat.retry()}>
                        Try again
                      </Button>
                    ) : undefined
                  }
                >
                  {turnError.message}
                </Banner>
              ) : null}
              {state.recovering && !state.streaming ? (
                <p role="status" className="flex items-center gap-2 text-small text-fg-muted">
                  <Spinner size={14} />
                  <span>Waiting for the assistant to finish its reply&hellip;</span>
                </p>
              ) : null}

              {guideText ? <p className="px-1 text-small text-fg-muted">{guideText}</p> : null}
              {showChips ? (
                <div className="flex flex-wrap gap-2" role="group" aria-label="Suggestions">
                  {chips.map(c => (
                    <Chip key={c} icon={<Sparkles />} className="max-w-full" onClick={() => submit(c)}>
                      <span className="min-w-0 truncate">{c}</span>
                    </Chip>
                  ))}
                </div>
              ) : null}

              <Composer
                ref={composerRef}
                value={draft}
                onChange={setDraft}
                onSend={submit}
                busy={chat.busy}
                onStop={state.streaming ? chat.stop : undefined}
                disabled={state.load !== 'ready'}
                placeholder={awaitingReply && chat.busy ? 'Waiting for the reply…' : KIND_PLACEHOLDERS[kind]}
                textareaLabel="Message the assistant"
                maxLength={4000}
                minRows={1}
                footerLeft={
                  <>
                    <span className="inline-flex h-8 items-center gap-1.5 rounded-pill bg-surface-muted px-3 text-small font-medium text-fg-muted">
                      <MessageSquare aria-hidden className="h-4 w-4" />
                      {KIND_LABELS[kind]}
                    </span>
                    {variant === 'page' && !isXl ? (
                      <Chip icon={<PanelLeft />} onClick={() => setSheetOpen(true)} aria-haspopup="dialog">
                        Study spec
                      </Chip>
                    ) : null}
                  </>
                }
              />
              <p className="px-2 text-center text-[11px] text-fg-subtle">
                Numbers come from the engines and appear in cards. Check them before you rely on them.
              </p>
            </div>
          </div>
        </div>

        {variant === 'page' && isXl ? (
          <aside aria-label="Study spec" className="w-80 flex-none overflow-y-auto border-l border-line bg-surface p-5">
            <SpecPanel spec={state.spec} />
          </aside>
        ) : null}
      </div>

      {variant === 'page' && !isXl ? (
        <Modal open={sheetOpen} onClose={() => setSheetOpen(false)} title="Study spec" size="sm">
          <SpecPanel spec={state.spec} heading={false} />
        </Modal>
      ) : null}
    </CardContext.Provider>
  )
})

/** A neutral "Back to Home" link for failed loads. */
export function BackToHome() {
  return (
    <ButtonLink as={Link} to="/app" variant="ghost">
      Back to Home
    </ButtonLink>
  )
}
