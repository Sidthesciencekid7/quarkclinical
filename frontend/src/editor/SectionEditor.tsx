import { useEffect, useRef, useState } from 'react'
import { BubbleMenu, EditorContent, useEditor } from '@tiptap/react'
import StarterKit from '@tiptap/starter-kit'
import Highlight from '@tiptap/extension-highlight'
import Typography from '@tiptap/extension-typography'
import Placeholder from '@tiptap/extension-placeholder'
import { api } from '../api/client'
import { Banner, Button, RefreshCw, Spinner } from '../ui'

interface Props {
  protocolId: string
  sectionName: string
  sectionTitle: string
  number: number
  initialContent: string
  onContentChange: (name: string, content: string) => void
  isRegenerating: boolean
  onRegenerate: () => void
  regenError?: string
}

type SaveStatus = 'saved' | 'saving' | 'unsaved' | 'error'

function textToHTML(text: string): string {
  if (!text?.trim()) return '<p></p>'
  if (text.trimStart().startsWith('<')) return text
  return text
    .trim()
    .split(/\n\n+/)
    .map(para => `<p>${para.replace(/\n/g, '<br>')}</p>`)
    .join('\n')
}

const SAVE_STATUS_STYLES: Record<SaveStatus, string> = {
  saving:  'text-fg-subtle',
  saved:   'text-[color:var(--ok-fg)]',
  unsaved: 'text-[color:var(--warn-fg)]',
  error:   'text-[color:var(--bad-fg)]',
}

const SAVE_STATUS_LABELS: Record<SaveStatus, string> = {
  saving: 'Saving…',
  saved: 'Saved',
  unsaved: 'Unsaved',
  error: 'Save failed',
}

/** Floating format toolbar (shown over a text selection): a dark pill. */
function ToolbarBtn({
  active, onClick, title, children,
}: {
  active: boolean; onClick: () => void; title: string; children: React.ReactNode
}) {
  return (
    <button
      type="button"
      onMouseDown={e => { e.preventDefault(); onClick() }}
      title={title}
      aria-label={title}
      aria-pressed={active}
      className={[
        'h-7 min-w-7 rounded-full px-2 text-small font-semibold transition-colors duration-fast',
        active ? 'bg-white/25' : 'hover:bg-white/15',
      ].join(' ')}
    >
      {children}
    </button>
  )
}

export default function SectionEditor({
  protocolId, sectionName, sectionTitle, number,
  initialContent, onContentChange, isRegenerating, onRegenerate, regenError,
}: Props) {
  const [saveStatus, setSaveStatus] = useState<SaveStatus>('saved')
  const saveTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  const editor = useEditor({
    extensions: [
      StarterKit.configure({ heading: { levels: [2, 3] }, code: false, codeBlock: false }),
      Highlight.configure({ multicolor: true }),
      Typography,
      Placeholder.configure({ placeholder: 'Not yet generated. Click Regenerate to produce content with AI.' }),
    ],
    content: textToHTML(initialContent),
    onUpdate({ editor }) {
      const html = editor.getHTML()
      onContentChange(sectionName, html)
      setSaveStatus('unsaved')
      if (saveTimerRef.current) clearTimeout(saveTimerRef.current)
      if (isRegenerating) return
      saveTimerRef.current = setTimeout(async () => {
        setSaveStatus('saving')
        try {
          await api.saveSection(protocolId, sectionName, html)
          setSaveStatus('saved')
        } catch {
          setSaveStatus('error')
        }
      }, 800)
    },
  })

  useEffect(() => {
    return () => { if (saveTimerRef.current) clearTimeout(saveTimerRef.current) }
  }, [])

  useEffect(() => {
    if (isRegenerating && saveTimerRef.current) {
      clearTimeout(saveTimerRef.current)
      saveTimerRef.current = null
      setSaveStatus('saved')
    }
  }, [isRegenerating])

  const wordCount = editor?.getText().split(/\s+/).filter(Boolean).length ?? 0

  return (
    <div id={`section-${sectionName}`} className="mb-10 scroll-mt-4">
      {/* Section header */}
      <div className="mb-3 flex flex-wrap items-start justify-between gap-x-4 gap-y-2 px-0.5">
        <div className="min-w-0">
          <p className="eyebrow">Section {number}</p>
          <h2 className="mt-0.5 text-h3 text-fg">{sectionTitle}</h2>
        </div>

        <div className="flex flex-none items-center gap-3">
          <span
            role={saveStatus === 'error' ? 'alert' : undefined}
            className={`min-w-[52px] text-right text-small font-medium ${SAVE_STATUS_STYLES[saveStatus]}`}
          >
            {SAVE_STATUS_LABELS[saveStatus]}
          </span>
          <span className="text-small tabular-nums text-fg-subtle">
            {wordCount.toLocaleString()} words
          </span>
          <Button
            variant="secondary"
            size="sm"
            onClick={onRegenerate}
            loading={isRegenerating}
            leftIcon={<RefreshCw aria-hidden />}
            aria-label={`Regenerate ${sectionTitle} with AI`}
          >
            Regenerate
          </Button>
        </div>
      </div>

      {regenError && (
        <Banner tone="danger" title="Regeneration failed" className="mb-3">
          {regenError}
        </Banner>
      )}

      {/* Editor card */}
      <div className="section-card relative overflow-hidden">
        {editor && (
          <BubbleMenu editor={editor} tippyOptions={{ duration: 80, placement: 'top' }}>
            <div role="toolbar" aria-label="Text formatting" className="flex items-center gap-0.5 rounded-full bg-ink-950 p-1 text-white shadow-pop">
              <ToolbarBtn active={editor.isActive('bold')} onClick={() => editor.chain().focus().toggleBold().run()} title="Bold">
                <span className="font-bold">B</span>
              </ToolbarBtn>
              <ToolbarBtn active={editor.isActive('italic')} onClick={() => editor.chain().focus().toggleItalic().run()} title="Italic">
                <span className="italic">I</span>
              </ToolbarBtn>
              <ToolbarBtn active={editor.isActive('highlight')} onClick={() => editor.chain().focus().toggleHighlight().run()} title="Highlight">
                H
              </ToolbarBtn>
              <div className="w-px bg-white/20 mx-0.5" />
              <ToolbarBtn active={editor.isActive('bulletList')} onClick={() => editor.chain().focus().toggleBulletList().run()} title="Bullet list">
                •—
              </ToolbarBtn>
              <ToolbarBtn active={editor.isActive('orderedList')} onClick={() => editor.chain().focus().toggleOrderedList().run()} title="Numbered list">
                1.
              </ToolbarBtn>
            </div>
          </BubbleMenu>
        )}

        {isRegenerating && (
          <div role="status" className="absolute inset-0 z-10 flex items-center justify-center gap-3 bg-white/90 backdrop-blur-[2px]">
            <Spinner size={20} className="text-accent-600" />
            <span className="text-body font-medium text-fg">Regenerating with AI…</span>
          </div>
        )}

        <div className="protocol-doc prose prose-slate max-w-none px-7 py-6">
          <EditorContent editor={editor} />
        </div>
      </div>
    </div>
  )
}
