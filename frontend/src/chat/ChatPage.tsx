import { Navigate, useParams } from 'react-router-dom'
import { crumbKey, useSetCrumb } from '../lib/breadcrumbs'
import { BackToHome, ChatView } from './ChatView'
import { KIND_LABELS } from './starters'
import { useChatSession } from './useChatSession'
import { useNotify } from './useNotify'

const MAX_CRUMB = 48

function ellipsize(text: string): string {
  const one = text.replace(/\s+/g, ' ').trim()
  return one.length > MAX_CRUMB ? `${one.slice(0, MAX_CRUMB - 1)}…` : one
}

function ChatSessionPage({ sessionId }: { sessionId: string }) {
  const notify = useNotify()
  const chat = useChatSession(sessionId, { notify })
  const { session, entries } = chat.state

  // The tab title and breadcrumb: the server's title, else the first thing the user said.
  const firstUser = entries.find(e => e.kind === 'message' && e.message.role === 'user')
  const firstUserText = firstUser && firstUser.kind === 'message' ? firstUser.message.text : ''
  const label = session?.title || firstUserText || (session ? `${KIND_LABELS[session.kind]} conversation` : '')
  useSetCrumb(crumbKey.chat(sessionId), label ? ellipsize(label) : null)
  useSetCrumb(crumbKey.chatKind(sessionId), session?.kind ?? null)

  return (
    <div className="h-[calc(100dvh-3.5rem)] min-h-0 w-full">
      <ChatView chat={chat} variant="page" autoFocus onLeave={<BackToHome />} />
    </div>
  )
}

/** `/app/chat/:sessionId`: one conversation. Keyed by session id so opening another one starts clean. */
export default function ChatPage() {
  const { sessionId } = useParams<{ sessionId: string }>()
  if (!sessionId) return <Navigate to="/app/chat/new" replace />
  return <ChatSessionPage key={sessionId} sessionId={sessionId} />
}
