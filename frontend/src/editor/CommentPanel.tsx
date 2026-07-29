import { useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import type { Comment, SectionName } from '../types'
import { SECTION_DISPLAY_NAMES } from '../types'

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
  // protocol on mount. Every failure path is swallowed into `error` — a broken
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
    // Optimistic: the checkbox should feel instant; revert if the server says no.
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
    <aside className="w-72 flex-shrink-0 flex flex-col bg-white border-l border-slate-200">
      {/* Header */}
      <div className="px-4 py-3 border-b border-slate-100 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="text-sm font-semibold text-slate-800">Review Notes</span>
          {unresolvedCount > 0 && (
            <span className="text-[10px] font-semibold bg-amber-100 text-amber-700 px-1.5 py-0.5 rounded-full">
              {unresolvedCount}
            </span>
          )}
        </div>
        <button
          onClick={() => setShowAll(s => !s)}
          className="text-[11px] text-slate-400 hover:text-slate-600 transition-colors"
        >
          {showAll ? 'This section' : 'All sections'}
        </button>
      </div>

      {/* Section label */}
      {!showAll && (
        <div className="px-4 py-2 bg-slate-50 border-b border-slate-100">
          <p className="text-[11px] text-slate-500">
            {'§ '}
            <span className="font-medium text-slate-700">
              {SECTION_DISPLAY_NAMES[activeSection as SectionName] ?? activeSection}
            </span>
          </p>
        </div>
      )}

      {/* Comment list */}
      <div className="flex-1 overflow-y-auto p-3 space-y-2">
        {loading ? (
          <div className="text-center py-10">
            <p className="text-xs text-slate-400">Loading notes…</p>
          </div>
        ) : sectionComments.length === 0 ? (
          <div className="text-center py-10">
            <svg
              width="24"
              height="24"
              viewBox="0 0 24 24"
              fill="none"
              className="mx-auto mb-2 text-slate-200"
            >
              <path
                d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2v10z"
                stroke="currentColor"
                strokeWidth="1.5"
                strokeLinejoin="round"
              />
            </svg>
            <p className="text-xs text-slate-400">No notes yet</p>
          </div>
        ) : (
          sectionComments.map(comment => (
            <div
              key={comment.id}
              className={[
                'rounded-xl p-3 border text-xs transition-opacity',
                comment.resolved
                  ? 'bg-slate-50 border-slate-100 opacity-60'
                  : 'bg-white border-slate-200',
              ].join(' ')}
            >
              <div className="flex items-center justify-between gap-2 mb-1.5">
                <span className="font-semibold text-slate-700">{comment.author}</span>
                <div className="flex items-center gap-1.5">
                  <span className="text-[10px] text-slate-400">{relativeTime(comment.createdAt)}</span>
                  <button
                    onClick={() => toggleResolved(comment.id)}
                    disabled={pendingIds.has(comment.id)}
                    title={comment.resolved ? 'Reopen' : 'Resolve'}
                    className={[
                      'w-4 h-4 rounded border flex items-center justify-center transition-colors',
                      comment.resolved
                        ? 'bg-emerald-100 border-emerald-300'
                        : 'border-slate-300 hover:border-emerald-400',
                      pendingIds.has(comment.id) ? 'opacity-40 cursor-not-allowed' : '',
                    ].join(' ')}
                  >
                    {comment.resolved && (
                      <svg width="8" height="8" viewBox="0 0 8 8" fill="none">
                        <path
                          d="M1 4l2 2 4-4"
                          stroke="#059669"
                          strokeWidth="1.5"
                          strokeLinecap="round"
                          strokeLinejoin="round"
                        />
                      </svg>
                    )}
                  </button>
                  <button
                    onClick={() => deleteComment(comment.id)}
                    className="text-slate-300 hover:text-red-400 transition-colors"
                    title="Delete"
                  >
                    <svg width="10" height="10" viewBox="0 0 10 10" fill="none">
                      <path
                        d="M2 2l6 6M8 2l-6 6"
                        stroke="currentColor"
                        strokeWidth="1.5"
                        strokeLinecap="round"
                      />
                    </svg>
                  </button>
                </div>
              </div>
              <p className="text-slate-600 leading-relaxed whitespace-pre-wrap">{comment.text}</p>
              {showAll && (
                <p className="text-[10px] text-indigo-400 mt-1.5 font-medium">
                  {'§ '}
                  {SECTION_DISPLAY_NAMES[comment.sectionName as SectionName] ?? comment.sectionName}
                </p>
              )}
            </div>
          ))
        )}
      </div>

      {/* Add note */}
      <div className="p-3 border-t border-slate-100">
        {error && (
          <p className="mb-2 text-[11px] text-red-500 leading-snug">{error}</p>
        )}
        <textarea
          ref={textareaRef}
          value={newText}
          onChange={e => setNewText(e.target.value)}
          onKeyDown={e => {
            if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) addComment()
          }}
          placeholder="Add a reviewer note…"
          rows={3}
          className="w-full px-3 py-2 text-xs text-slate-700 placeholder-slate-400 border border-slate-200 rounded-lg resize-none focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:border-transparent"
        />
        <button
          onClick={addComment}
          disabled={!newText.trim() || posting}
          className="mt-2 w-full py-1.5 text-xs font-semibold bg-slate-800 text-white rounded-lg hover:bg-slate-700 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
        >
          {posting ? 'Posting…' : 'Post Note'}
          <span className="ml-1 opacity-40 font-normal">⌘↵</span>
        </button>
      </div>
    </aside>
  )
}
