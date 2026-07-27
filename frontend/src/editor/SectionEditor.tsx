import { useEffect, useRef, useState } from 'react'
import { BubbleMenu, EditorContent, useEditor } from '@tiptap/react'
import StarterKit from '@tiptap/starter-kit'
import Highlight from '@tiptap/extension-highlight'
import Typography from '@tiptap/extension-typography'
import Placeholder from '@tiptap/extension-placeholder'
import { api } from '../api/client'

interface Props {
  protocolId: string
  sectionName: string
  sectionTitle: string
  number: number
  initialContent: string
  onContentChange: (name: string, content: string) => void
  isRegenerating: boolean
  onRegenerate: () => void
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
  saving:  'text-slate-400',
  saved:   'text-emerald-500',
  unsaved: 'text-amber-500',
  error:   'text-red-500',
}

const SAVE_STATUS_LABELS: Record<SaveStatus, string> = {
  saving: 'Saving…',
  saved: 'Saved',
  unsaved: 'Unsaved',
  error: 'Save failed',
}

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
      className={[
        'rounded px-1.5 py-0.5 text-xs font-semibold transition-colors',
        active ? 'bg-white/20' : 'hover:bg-white/10',
      ].join(' ')}
    >
      {children}
    </button>
  )
}

export default function SectionEditor({
  protocolId, sectionName, sectionTitle, number,
  initialContent, onContentChange, isRegenerating, onRegenerate,
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

  const wordCount = editor?.getText().split(/\s+/).filter(Boolean).length ?? 0

  return (
    <div id={`section-${sectionName}`} className="mb-10 scroll-mt-4">
      {/* Section header */}
      <div className="flex items-start justify-between mb-3 px-0.5">
        <div>
          <div className="flex items-center gap-2 mb-1">
            <span className="inline-flex items-center justify-center w-6 h-6 rounded-lg bg-gradient-to-br from-cyan-500 to-teal-600 text-white text-[10px] font-bold flex-shrink-0 shadow-sm shadow-cyan-600/25">
              {number}
            </span>
            <span className="text-[10px] font-bold text-cyan-700/80 uppercase tracking-[0.15em]">Section {number}.0</span>
          </div>
          <h2 className="text-[17px] font-bold text-slate-900 leading-snug tracking-tight ml-8">{sectionTitle}</h2>
        </div>

        <div className="flex items-center gap-3 flex-shrink-0 mt-1">
          <span className={`text-[11px] font-semibold min-w-[52px] text-right ${SAVE_STATUS_STYLES[saveStatus]}`}>
            {SAVE_STATUS_LABELS[saveStatus]}
          </span>
          <span className="text-[11px] text-slate-400 font-medium">
            {wordCount.toLocaleString()} words
          </span>
          <button
            onClick={onRegenerate}
            disabled={isRegenerating}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-bold border border-cyan-200 text-cyan-600 rounded-lg hover:bg-cyan-50 hover:border-cyan-300 disabled:opacity-50 disabled:cursor-not-allowed transition-all duration-150 cursor-pointer"
          >
            <svg width="11" height="11" viewBox="0 0 11 11" fill="none" className={isRegenerating ? 'animate-spin' : ''}>
              <path d="M9.5 5.5A4 4 0 111.5 5.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
              <path d="M9.5 3v2.5H7" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
            Regenerate
          </button>
        </div>
      </div>

      {/* Editor card */}
      <div className="section-card relative overflow-hidden">
        {editor && (
          <BubbleMenu editor={editor} tippyOptions={{ duration: 80, placement: 'top' }}>
            <div className="flex gap-0.5 bg-slate-900 text-white rounded-lg p-1 shadow-xl text-xs">
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
          <div className="absolute inset-0 z-10 bg-white/90 backdrop-blur-[2px] flex items-center justify-center gap-3">
            <svg width="20" height="20" viewBox="0 0 20 20" fill="none" className="animate-spin">
              <circle cx="10" cy="10" r="8" stroke="#e0f2fe" strokeWidth="3" />
              <path d="M10 2a8 8 0 018 8" stroke="#0891b2" strokeWidth="3" strokeLinecap="round" />
            </svg>
            <span className="text-sm font-semibold text-slate-700">Regenerating with AI…</span>
          </div>
        )}

        <div className="protocol-doc prose prose-slate max-w-none px-7 py-6">
          <EditorContent editor={editor} />
        </div>
      </div>
    </div>
  )
}
