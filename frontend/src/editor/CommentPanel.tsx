import { useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import type { Comment, SectionName } from '../types'
import { SECTION_DISPLAY_NAMES } from '../types'
import { Badge, Banner, Button, Check, IconButton, MessageSquare, PillNav, Textarea, Trash2, cx } from '../ui'
import { PANEL_CLASS } from './PanelShell'

interface Props {
  protocolId: string
  activeSection: string
}

function relativeTime(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime()
  const min = Math.floor(diff / 60000)
  if (min < 1) return 'just now'
  if (min < 60) return `${min}m ago`
  const hrs = Math.floor(min / 60)
  if (hrs < 24) return `${hrs}h ago`
  return `${Math.floor(hrs / 24)}d ago`
}

const SCOPE_ITEMS = [
  { id: 'section', label: 'This section' },
  { id: 'all', label: 'All sections' },
]

export default function CommentPanel({ protocolId, activeSection }: Props) {
  const [comments, setComments] = useState<Comment[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [newText, setNewText] = useState('')
  const [showAll, setShowAll] = useState(false)
  const [posting, setPosting] = useState(false)
  const [pendingIds, setPendingIds] = useState<Set<string>>(new Set())
  const textareaRef = useRef<HTMLTextAreaElement>(null)

  // Notes live server-side (org-shared, user-attributed), so load them for this
  // protocol on mount. Every failure path is swallowed into `error`: a broken
  // notes panel must never take the editor down with it.
  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    api
      .listComments(protocolId)
      .then(rows => {
        if (!cancelled) setComments(rows)
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(e instanceof Error ? e.message : 'Could not load notes')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [protocolId])

  const sectionComments = showAll
    ? comments
    : comments.filter(c => c.sectionName === activeSection)

  const unresolvedCount = comments.filter(c => !c.resolved).length

  const addComment = async () => {
    const text = newText.trim()
    if (!text || posting) return
    setPosting(true)
    setError(null)
    try {
      const created = await api.createComment(protocolId, activeSection, text)
      setComments(prev => [created, ...prev])
      setNewText('')
      textareaRef.current?.focus()
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Could not post note')
    } finally {
      setPosting(false)
    }
  }

  const toggleResolved = async (id: string) => {
    if (pendingIds.has(id)) return
    const target = comments.find(c => c.id === id)
    if (!target) return
    const next = !target.resolved
    setPendingIds(prev => new Set(prev).add(id))
    // Optimistic: the toggle should feel instant; revert if the server says no.
    setComments(prev => prev.map(c => (c.id === id ? { ...c, resolved: next } : c)))
    try {
      await api.updateComment(protocolId, id, next)
    } catch (e: unknown) {
      setComments(prev => prev.map(c => (c.id === id ? { ...c, resolved: !next } : c)))
      setError(e instanceof Error ? e.message : 'Could not update note')
    } finally {
      setPendingIds(prev => {
        const copy = new Set(prev)
        copy.delete(id)
        return copy
      })
    }
  }

  const deleteComment = async (id: string) => {
    const snapshot = comments
    setComments(prev => prev.filter(c => c.id !== id))
    try {
      await api.deleteComment(protocolId, id)
    } catch (e: unknown) {
      setComments(snapshot)
      setError(e instanceof Error ? e.message : 'Could not delete note')
    }
  }

  return (
    <aside aria-label="Review notes" className={PANEL_CLASS}>
      {/* Header */}
      <div className="flex-none border-b border-line px-4 pb-3 pt-4">
        <div className="flex items-center justify-between gap-2">
          <div className="flex min-w-0 items-center gap-2.5">
            <span
              aria-hidden
              className="flex h-8 w-8 flex-none items-center justify-center rounded-xl bg-surface-muted text-fg-muted [&>svg]:h-4 [&>svg]:w-4"
            >
              <MessageSquare />
            </span>
            <h2 className="truncate text-h3 text-fg">Review notes</h2>
            {unresolvedCount > 0 ? (
              <Badge variant="warning" aria-label={`${unresolvedCount} unresolved`}>
                {unresolvedCount}
              </Badge>
            ) : null}
          </div>
        </div>
        <div className="mt-3">
          <PillNav
            size="sm"
            ariaLabel="Notes scope"
            items={SCOPE_ITEMS}
            value={showAll ? 'all' : 'section'}
            onChange={id => setShowAll(id === 'all')}
          />
        </div>
        {!showAll ? (
          <p className="mt-2.5 text-small text-fg-muted">
            Section:{' '}
            <span className="font-medium text-fg">
              {SECTION_DISPLAY_NAMES[activeSection as SectionName] ?? activeSection}
            </span>
          </p>
        ) : null}
      </div>

      {/* Comment list */}
      <div className="min-h-0 flex-1 space-y-2 overflow-y-auto p-3">
        {loading ? (
          <p className="py-10 text-center text-small text-fg-subtle" role="status">
            Loading notes…
          </p>
        ) : sectionComments.length === 0 ? (
          <div className="py-10 text-center">
            <MessageSquare aria-hidden className="mx-auto mb-2 h-6 w-6 text-ink-300" />
            <p className="text-small text-fg-subtle">No notes yet</p>
          </div>
        ) : (
          sectionComments.map(comment => (
            <article
              key={comment.id}
              className={cx(
                'card p-3 text-small transition-opacity duration-fast',
                comment.resolved && 'bg-surface-muted opacity-70',
              )}
            >
              <div className="mb-1.5 flex items-center justify-between gap-2">
                <span className="font-semibold text-fg">{comment.author}</span>
                <div className="flex items-center gap-0.5">
                  <span className="mr-1 text-[11px] text-fg-subtle">{relativeTime(comment.createdAt)}</span>
                  <IconButton
                    size="sm"
                    label="Mark as resolved"
                    aria-pressed={comment.resolved}
                    icon={<Check />}
                    disabled={pendingIds.has(comment.id)}
                    onClick={() => toggleResolved(comment.id)}
                    className={cx(comment.resolved && '!bg-[var(--ok-bg)] !text-[color:var(--ok-fg)]')}
                  />
                  <IconButton
                    size="sm"
                    label="Delete note"
                    icon={<Trash2 />}
                    onClick={() => deleteComment(comment.id)}
                  />
                </div>
              </div>
              <p className="whitespace-pre-wrap text-fg-muted">{comment.text}</p>
              {showAll ? (
                <p className="mt-1.5 text-[11px] font-medium text-fg-subtle">
                  {SECTION_DISPLAY_NAMES[comment.sectionName as SectionName] ?? comment.sectionName}
                </p>
              ) : null}
            </article>
          ))
        )}
      </div>

      {/* Add note */}
      <div className="flex-none space-y-2 border-t border-line p-3">
        {error ? <Banner tone="danger">{error}</Banner> : null}
        <Textarea
          ref={textareaRef}
          aria-label="Add a reviewer note"
          value={newText}
          onChange={e => setNewText(e.target.value)}
          onKeyDown={e => {
            if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) addComment()
          }}
          placeholder="Add a reviewer note…"
          rows={3}
        />
        <Button fullWidth onClick={addComment} loading={posting} disabled={!newText.trim()}>
          {posting ? 'Posting…' : 'Post note'}
        </Button>
        <p className="text-center text-[11px] text-fg-subtle">Ctrl or Cmd + Enter also posts</p>
      </div>
    </aside>
  )
}
