import { useCallback, useEffect, useRef, useState } from 'react'
import type { KeyboardEvent } from 'react'
import { createPortal } from 'react-dom'
import { createChat, listChats } from '../api/chat'
import { Banner, Button, IconButton, Spinner, X } from '../ui'
import { getFocusable } from '../ui/cx'
import { ChatView } from './ChatView'
import { useChatSession } from './useChatSession'
import { useNotify } from './useNotify'
import type { ActionType } from './types'

/**
 * Find the newest active `protocol_edit` conversation for a protocol, or start
 * one. Exported for tests; the drawer is its only caller.
 */
export async function findOrCreateEditSession(protocolId: string): Promise<string> {
  const items = await listChats({ kind: 'protocol_edit', limit: 100 })
  const mine = (Array.isArray(items) ? items : [])
    .filter(i => i.protocol_id === protocolId && i.status === 'active')
    .sort((a, b) => String(b.last_activity_at).localeCompare(String(a.last_activity_at)))
  if (mine.length > 0) return mine[0].id
  const created = await createChat({ kind: 'protocol_edit', protocol_id: protocolId })
  return created.id
}

export interface ChatDrawerProps {
  /** The protocol the conversation edits. */
  protocolId: string
  open: boolean
  onClose: () => void
  /** Called after an edit was applied, so the editor can reload the protocol. */
  onApplied?: () => void
  /** Heading (default "Edit with AI"). */
  title?: string
}

type Lookup = { state: 'idle' } | { state: 'finding' } | { state: 'ready'; sessionId: string } | { state: 'error'; message: string }

function DrawerConversation({ sessionId, onApplied }: { sessionId: string; onApplied?: () => void }) {
  const notify = useNotify()
  const onAppliedRef = useRef(onApplied)
  onAppliedRef.current = onApplied
  const chat = useChatSession(sessionId, {
    notify,
    onActionSuccess: (action: ActionType) => {
      if (action === 'apply_edit') onAppliedRef.current?.()
    },
  })
  return <ChatView chat={chat} kind="protocol_edit" variant="drawer" autoFocus />
}

/**
 * Right-hand drawer hosting the edit conversation for one protocol. A modal
 * dialog: focus moves in on open and is trapped, Esc or a click on the dimmed
 * backdrop closes it, and focus returns to whatever opened it. The
 * conversation is found (or created) when the drawer first opens for a
 * protocol and then reused.
 */
export function ChatDrawer({ protocolId, open, onClose, onApplied, title = 'Edit with AI' }: ChatDrawerProps) {
  const [lookup, setLookup] = useState<Lookup>({ state: 'idle' })
  const lookupFor = useRef<string | null>(null)
  const panelRef = useRef<HTMLDivElement>(null)
  const onCloseRef = useRef(onClose)
  onCloseRef.current = onClose

  const find = useCallback(() => {
    lookupFor.current = protocolId
    setLookup({ state: 'finding' })
    findOrCreateEditSession(protocolId)
      .then(sessionId => {
        if (lookupFor.current === protocolId) setLookup({ state: 'ready', sessionId })
      })
      .catch(e => {
        if (lookupFor.current === protocolId) {
          lookupFor.current = null // allow a retry
          setLookup({ state: 'error', message: e instanceof Error ? e.message : 'Could not open the editing conversation.' })
        }
      })
  }, [protocolId])

  // Find-or-create once per protocol, only after the drawer has been opened.
  useEffect(() => {
    if (!open) return
    if (lookupFor.current === protocolId) return // found, or already looking (StrictMode runs effects twice)
    find()
  }, [open, protocolId, find])

  // Dialog behaviour: scroll lock, focus in, focus back, Esc.
  useEffect(() => {
    if (!open) return
    const opener = document.activeElement as HTMLElement | null
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'

    const panel = panelRef.current
    const reduceMotion = typeof window.matchMedia === 'function' && window.matchMedia('(prefers-reduced-motion: reduce)').matches
    if (panel && !reduceMotion && typeof panel.animate === 'function') {
      panel.animate([{ transform: 'translateX(100%)' }, { transform: 'translateX(0)' }], { duration: 200, easing: 'cubic-bezier(0.16, 1, 0.3, 1)' })
    }
    ;(getFocusable(panel)[0] ?? panel)?.focus()

    return () => {
      document.body.style.overflow = previousOverflow
      if (opener && document.contains(opener)) opener.focus()
    }
  }, [open])

  if (!open) return null

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key === 'Escape') {
      e.preventDefault()
      e.stopPropagation()
      onCloseRef.current()
      return
    }
    if (e.key !== 'Tab') return
    const focusable = getFocusable(panelRef.current)
    if (focusable.length === 0) {
      e.preventDefault()
      panelRef.current?.focus()
      return
    }
    const first = focusable[0]
    const last = focusable[focusable.length - 1]
    const active = document.activeElement
    if (e.shiftKey && (active === first || active === panelRef.current)) {
      e.preventDefault()
      last.focus()
    } else if (!e.shiftKey && active === last) {
      e.preventDefault()
      first.focus()
    } else if (!panelRef.current?.contains(active)) {
      e.preventDefault()
      first.focus()
    }
  }

  return createPortal(
    <div className="fixed inset-0 z-[70] flex justify-end" data-testid="chat-drawer">
      <div aria-hidden className="absolute inset-0 bg-overlay animate-ui-fade-in" onMouseDown={onClose} />
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        tabIndex={-1}
        onKeyDown={onKeyDown}
        className="relative flex h-full w-[min(100vw,30rem)] flex-col border-l border-line bg-surface text-fg shadow-pop outline-none"
      >
        <div className="flex flex-none items-center gap-3 border-b border-line px-4 py-3">
          <div className="min-w-0 flex-1">
            <h2 className="text-h3 text-fg">{title}</h2>
            <p className="text-small text-fg-muted">Changes are proposed first. Nothing is applied until you say so.</p>
          </div>
          <IconButton label="Close editing assistant" icon={<X />} size="sm" onClick={onClose} />
        </div>

        <div className="min-h-0 flex-1">
          {lookup.state === 'ready' ? (
            <DrawerConversation key={lookup.sessionId} sessionId={lookup.sessionId} onApplied={onApplied} />
          ) : lookup.state === 'error' ? (
            <div className="space-y-3 p-4">
              <Banner tone="danger" title="Could not open the editing assistant">
                {lookup.message}
              </Banner>
              <Button variant="secondary" onClick={find}>
                Try again
              </Button>
            </div>
          ) : (
            <div role="status" className="flex items-center justify-center gap-2 py-16 text-body text-fg-muted">
              <Spinner size={18} />
              <span>Opening the editing assistant&hellip;</span>
            </div>
          )}
        </div>
      </div>
    </div>,
    document.body,
  )
}
