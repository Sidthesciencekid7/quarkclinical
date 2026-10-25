import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { createChat } from '../api/chat'
import { clearPendingPrompt, loadPendingPrompt } from '../lib/pendingPrompt'
import { clearHomeDraft, loadHomeDraft } from '../shell/homeDraft'
import { Banner, Button, ButtonLink, Calculator, Chip, Composer, EmptyState, FileText, PillNav, Sparkles, Spinner } from '../ui'
import { KIND_EMPTY_BODY, KIND_EMPTY_TITLES, KIND_PLACEHOLDERS, STARTER_CHIPS, isSessionKind, presetFor } from './starters'
import type { SessionKind } from './types'

/** What a new conversation starts from (a saved description, a preset tile, a protocol to price or edit). */
export interface StartPlan {
  kind: SessionKind
  initialMessage: string | null
  protocolId: string | null
  /** Which stored draft to clear once the session exists. */
  clear: 'landing' | 'home' | null
}

export interface PlanInput {
  kind: string | null
  preset: string | null
  from: string | null
  protocolId: string | null
}

const PRICE_LINKED = 'I want to create a budget for my linked protocol.'

/**
 * Decide what `/app/chat/new?...` should do. Pure apart from reading the two
 * draft stores. `null` means "nothing to start from": show the start screen and
 * wait for the user to type.
 *  - from=landing: the text parked by the public landing page (localStorage).
 *  - from=home:    the text typed in the Home composer (sessionStorage).
 *  - preset=...:   a Home tile / pinned item, which says its starter sentence.
 *  - kind=budget&protocol_id=...: "Price this study" from a finished protocol.
 *  - kind=protocol_edit&protocol_id=...: edit a protocol (no message yet).
 */
export function planNewChat(input: PlanInput): StartPlan | null {
  const kindParam = isSessionKind(input.kind) ? input.kind : null
  const preset = presetFor(input.preset)
  const protocolId = input.protocolId?.trim() || null

  if (input.from === 'landing') {
    const pending = loadPendingPrompt()
    if (pending) return { kind: kindParam ?? pending.kind, initialMessage: pending.text, protocolId: null, clear: 'landing' }
  }
  if (input.from === 'home') {
    const draft = loadHomeDraft()
    if (draft) return { kind: kindParam ?? draft.kind, initialMessage: draft.text, protocolId: null, clear: 'home' }
  }
  if (preset) return { kind: preset.kind, initialMessage: preset.text, protocolId: null, clear: null }
  if (kindParam === 'budget' && protocolId) return { kind: 'budget', initialMessage: PRICE_LINKED, protocolId, clear: null }
  if (kindParam === 'protocol_edit' && protocolId) return { kind: 'protocol_edit', initialMessage: null, protocolId, clear: null }
  return null
}

type Phase = { name: 'idle' } | { name: 'creating' } | { name: 'error'; message: string }

/**
 * `/app/chat/new`: creates the session (no model call) and replaces the URL
 * with `/app/chat/:id`, where the first turn runs (the stored message is
 * unanswered, so the conversation page resumes it). With nothing to start from
 * it shows a start screen instead; sending the first message creates the
 * session the same way.
 */
export default function NewChatPage() {
  const [params] = useSearchParams()
  const navigate = useNavigate()

  const input: PlanInput = {
    kind: params.get('kind'),
    preset: params.get('preset'),
    from: params.get('from'),
    protocolId: params.get('protocol_id'),
  }
  const inputKey = `${input.kind}|${input.preset}|${input.from}|${input.protocolId}`

  const [phase, setPhase] = useState<Phase>({ name: 'idle' })
  const mountedRef = useRef(true)
  const startedFor = useRef<string | null>(null)
  const planRef = useRef<{ key: string; plan: StartPlan | null } | null>(null)

  // Read the stored drafts once per distinct URL (not on every render).
  if (!planRef.current || planRef.current.key !== inputKey) {
    planRef.current = { key: inputKey, plan: planNewChat(input) }
  }
  const plan = planRef.current.plan

  const start = useCallback(
    async (p: StartPlan) => {
      setPhase({ name: 'creating' })
      try {
        const session = await createChat({
          kind: p.kind,
          initial_message: p.initialMessage,
          ...(p.protocolId && p.kind !== 'protocol' ? { protocol_id: p.protocolId } : {}),
        })
        // Only now is the text safely on the server: forget the local copy.
        if (p.clear === 'landing') clearPendingPrompt()
        if (p.clear === 'home') clearHomeDraft()
        if (mountedRef.current) navigate(`/app/chat/${encodeURIComponent(session.id)}`, { replace: true })
      } catch (e) {
        if (mountedRef.current) setPhase({ name: 'error', message: e instanceof Error ? e.message : 'Could not start the conversation.' })
      }
    },
    [navigate],
  )

  useEffect(() => {
    mountedRef.current = true
    if (plan && startedFor.current !== inputKey) {
      startedFor.current = inputKey // StrictMode runs this effect twice: create the session once
      void start(plan)
    }
    return () => {
      mountedRef.current = false
    }
  }, [plan, inputKey, start])

  if (plan) {
    if (phase.name === 'error') {
      return (
        <div className="mx-auto w-full max-w-xl px-4 py-12">
          <Banner tone="danger" title="Could not start the conversation">
            {phase.message}
          </Banner>
          <div className="mt-4 flex gap-2">
            <Button variant="primary" onClick={() => void start(plan)}>
              Try again
            </Button>
            <ButtonLink as={Link} to="/app" variant="ghost">
              Back to Home
            </ButtonLink>
          </div>
        </div>
      )
    }
    return (
      <div className="flex min-h-[50vh] items-center justify-center gap-2 text-body text-fg-muted" role="status">
        <Spinner size={18} />
        <span>Starting your conversation&hellip;</span>
      </div>
    )
  }

  return (
    <StartScreen
      initialKind={isSessionKind(input.kind) && input.kind !== 'protocol_edit' ? input.kind : 'protocol'}
      missingDraft={input.from === 'landing' || input.from === 'home'}
      busy={phase.name === 'creating'}
      error={phase.name === 'error' ? phase.message : null}
      onStart={(kind, text) => void start({ kind, initialMessage: text, protocolId: null, clear: null })}
    />
  )
}

function StartScreen({
  initialKind,
  missingDraft,
  busy,
  error,
  onStart,
}: {
  initialKind: SessionKind
  missingDraft: boolean
  busy: boolean
  error: string | null
  onStart: (kind: SessionKind, text: string) => void
}) {
  const [kind, setKind] = useState<SessionKind>(initialKind)
  const [text, setText] = useState('')
  const chips = useMemo(() => STARTER_CHIPS[kind], [kind])
  const ref = useRef<HTMLTextAreaElement | null>(null)

  useEffect(() => {
    ref.current?.focus()
  }, [])

  const submit = (value: string) => {
    if (busy) return
    onStart(kind, value)
  }

  return (
    <div className="flex h-[calc(100dvh-3.5rem)] min-h-0 flex-col">
      <div className="min-h-0 flex-1 overflow-y-auto px-4">
        <div className="mx-auto flex min-h-full w-full max-w-3xl flex-col justify-center py-10">
          {missingDraft ? (
            <div className="mb-6">
              <Banner tone="info">Your saved description is no longer available in this browser. Describe your study below to begin.</Banner>
            </div>
          ) : null}
          <EmptyState icon={<Sparkles />} title={KIND_EMPTY_TITLES[kind]} description={KIND_EMPTY_BODY[kind]} />
        </div>
      </div>

      <div className="flex-none bg-surface px-4 pb-[max(1rem,env(safe-area-inset-bottom))]">
        <div className="mx-auto w-full max-w-3xl space-y-2.5">
          {error ? (
            <Banner tone="danger" title="Could not start the conversation">
              {error}
            </Banner>
          ) : null}
          <div className="flex flex-wrap gap-2" role="group" aria-label="Suggestions">
            {chips.map(c => (
              <Chip key={c} icon={<Sparkles />} className="max-w-full" disabled={busy} onClick={() => submit(c)}>
                <span className="min-w-0 truncate">{c}</span>
              </Chip>
            ))}
          </div>
          <Composer
            ref={ref}
            value={text}
            onChange={setText}
            onSend={v => {
              setText('')
              submit(v)
            }}
            busy={busy}
            placeholder={KIND_PLACEHOLDERS[kind]}
            textareaLabel="Describe your study"
            maxLength={4000}
            footerLeft={
              <PillNav
                ariaLabel="What to set up"
                size="sm"
                value={kind}
                onChange={id => setKind(id as SessionKind)}
                items={[
                  { id: 'protocol', label: 'Protocol', icon: <FileText /> },
                  { id: 'budget', label: 'Budget', icon: <Calculator /> },
                ]}
              />
            }
          />
        </div>
      </div>
    </div>
  )
}
