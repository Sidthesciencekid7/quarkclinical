import { useCallback, useMemo, useRef, useState } from 'react'
import type { Drug, GeneratedProtocol, SectionName, StudyType } from '../types'
import { SECTION_DISPLAY_NAMES, visibleSections } from '../types'
import { api } from '../api/client'
import SectionEditor from './SectionEditor'
import SectionSidebar from './SectionSidebar'
import CommentPanel from './CommentPanel'
import SourcesPanel from './SourcesPanel'
import QCPanel from './QCPanel'
import CoveragePanel from './CoveragePanel'
import RiskPanel from './RiskPanel'
import ExportButton from './ExportButton'

const STUDY_LABELS: Record<StudyType, string> = {
  BE: 'Bioequivalence',
  biosimilar_pk: 'Biosimilar PK',
  food_effect: 'Food Effect',
}

const STUDY_BADGES: Record<StudyType, { label: string; cls: string }> = {
  BE: { label: 'Small Molecule', cls: 'bg-cyan-500/15 text-cyan-300 border-cyan-500/20' },
  biosimilar_pk: { label: 'Biologic', cls: 'bg-violet-500/15 text-violet-300 border-violet-500/20' },
  food_effect: { label: 'Food Effect', cls: 'bg-amber-500/15 text-amber-300 border-amber-500/20' },
}

interface Props {
  protocol: GeneratedProtocol
  drug: Drug
  onBack: () => void
}

export default function ProtocolEditor({ protocol, drug, onBack }: Props) {
  // Sections shown depend on study type (immunogenicity is biosimilar-only).
  const order = useMemo(() => visibleSections(protocol.study_type), [protocol.study_type])

  const [sections, setSections] = useState<Record<string, string>>(() =>
    Object.fromEntries(order.map(name => [name, protocol.sections[name] ?? ''])),
  )

  const [contentVersions, setContentVersions] = useState<Record<string, number>>(() =>
    Object.fromEntries(order.map(name => [name, 0])),
  )

  const [regenerating, setRegenerating] = useState<string | null>(null)
  const [regenError, setRegenError] = useState<Record<string, string>>({})
  const [activeSection, setActiveSection] = useState<string>(order[0])
  const [commentsOpen, setCommentsOpen] = useState(false)
  const [qcOpen, setQcOpen] = useState(false)
  const [coverageOpen, setCoverageOpen] = useState(false)
  const [riskOpen, setRiskOpen] = useState(false)
  const [sourcesOpen, setSourcesOpen] = useState(false)
  const mainRef = useRef<HTMLDivElement>(null)

  const handleScroll = useCallback(() => {
    for (let i = order.length - 1; i >= 0; i--) {
      const el = document.getElementById(`section-${order[i]}`)
      if (!el) continue
      if (el.getBoundingClientRect().top <= 100) {
        setActiveSection(order[i])
        return
      }
    }
    setActiveSection(order[0])
  }, [order])

  const scrollToSection = useCallback((name: string) => {
    document.getElementById(`section-${name}`)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
    setActiveSection(name)
  }, [])

  const handleRegenerate = useCallback(async (sectionName: string) => {
    setRegenerating(sectionName)
    setRegenError(prev => {
      const { [sectionName]: _omit, ...rest } = prev
      return rest
    })
    try {
      const result = await api.regenSection(protocol.id, sectionName)
      setSections(prev => ({ ...prev, [sectionName]: result.content }))
      setContentVersions(prev => ({ ...prev, [sectionName]: prev[sectionName] + 1 }))
    } catch (err) {
      console.error(`Regeneration failed for ${sectionName}:`, err)
      setRegenError(prev => ({ ...prev, [sectionName]: err instanceof Error ? err.message : 'Regeneration failed. Please try again.' }))
    } finally {
      setRegenerating(null)
    }
  }, [protocol.id])

  const handleContentChange = useCallback((name: string, content: string) => {
    setSections(prev => ({ ...prev, [name]: content }))
  }, [])

  const completedCount = order.filter(n => sections[n]?.trim()).length
  const badge = STUDY_BADGES[protocol.study_type]

  return (
    <div className="flex flex-col" style={{ height: '100vh', overflow: 'hidden' }}>
      {/* ── Header ─────────────────────────────────────────────────── */}
      <header
        className="flex-shrink-0 h-14 border-b border-white/8 flex items-center px-4 gap-3 z-20"
        style={{ background: 'var(--navy)' }}
      >
        <button
          onClick={onBack}
          className="flex items-center gap-1.5 text-xs text-slate-400 hover:text-white transition-colors px-2.5 py-1.5 rounded-lg hover:bg-white/8 cursor-pointer"
          title="Back to setup"
        >
          <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
            <path d="M9 3L4 7l5 4" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
          <span className="hidden sm:inline font-medium">Setup</span>
        </button>

        <div className="w-px h-4 bg-white/10" />

        {/* Drug / study */}
        <div className="flex items-center gap-2.5 min-w-0 flex-1">
          <span className="font-bold text-sm text-white capitalize truncate">{drug.inn}</span>
          <span className="text-white/20 flex-shrink-0">·</span>
          <span className="text-xs text-slate-400 truncate hidden sm:block font-medium">
            {STUDY_LABELS[protocol.study_type]}
          </span>
          {badge && (
            <span className={`flex-shrink-0 text-[10px] font-bold border px-1.5 py-0.5 rounded-md ${badge.cls}`}>
              {badge.label}
            </span>
          )}
          <span className="flex-shrink-0 text-[10px] bg-white/8 text-slate-400 px-2 py-0.5 rounded-md font-medium border border-white/10">
            v{protocol.version} · {completedCount}/{order.length}
          </span>
        </div>

        {/* Actions */}
        <div className="flex items-center gap-2 flex-shrink-0">
          <button
            onClick={() => { setQcOpen(o => !o); setCommentsOpen(false); setCoverageOpen(false); setRiskOpen(false) }}
            className={[
              'flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold border transition-colors cursor-pointer',
              qcOpen
                ? 'bg-cyan-500/15 text-cyan-300 border-cyan-500/25'
                : 'border-white/10 text-slate-400 hover:bg-white/8 hover:text-slate-200',
            ].join(' ')}
            title="Run deterministic QC checks"
          >
            <svg width="12" height="12" viewBox="0 0 14 14" fill="none">
              <path d="M7 1l5 2v3.5C12 9.5 9.8 12 7 13 4.2 12 2 9.5 2 6.5V3l5-2z" stroke="currentColor" strokeWidth="1.2" strokeLinejoin="round" />
              <path d="M4.8 7l1.6 1.6L9.4 5.3" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
            QC
          </button>
          <button
            onClick={() => { setCoverageOpen(o => !o); setQcOpen(false); setCommentsOpen(false); setRiskOpen(false) }}
            className={[
              'flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold border transition-colors cursor-pointer',
              coverageOpen
                ? 'bg-cyan-500/15 text-cyan-300 border-cyan-500/25'
                : 'border-white/10 text-slate-400 hover:bg-white/8 hover:text-slate-200',
            ].join(' ')}
            title="Regulatory guideline coverage"
          >
            <svg width="12" height="12" viewBox="0 0 14 14" fill="none">
              <path d="M2.5 3h9M2.5 7h9M2.5 11h6" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" />
            </svg>
            Coverage
          </button>
          <button
            onClick={() => { setRiskOpen(o => !o); setQcOpen(false); setCoverageOpen(false); setCommentsOpen(false) }}
            className={[
              'flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold border transition-colors cursor-pointer',
              riskOpen
                ? 'bg-cyan-500/15 text-cyan-300 border-cyan-500/25'
                : 'border-white/10 text-slate-400 hover:bg-white/8 hover:text-slate-200',
            ].join(' ')}
            title="Operational feasibility risk score"
          >
            <svg width="12" height="12" viewBox="0 0 14 14" fill="none">
              <path d="M7 1.6l5.4 10H1.6L7 1.6z" stroke="currentColor" strokeWidth="1.2" strokeLinejoin="round" />
              <path d="M7 5.6v2.6M7 10.1v.01" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" />
            </svg>
            Risk
          </button>
          <button
            onClick={() => { setCommentsOpen(o => !o); setQcOpen(false); setCoverageOpen(false); setRiskOpen(false) }}
            className={[
              'flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold border transition-colors cursor-pointer',
              commentsOpen
                ? 'bg-cyan-500/15 text-cyan-300 border-cyan-500/25'
                : 'border-white/10 text-slate-400 hover:bg-white/8 hover:text-slate-200',
            ].join(' ')}
          >
            <svg width="12" height="12" viewBox="0 0 12 12" fill="none">
              <path d="M11 7.5a1 1 0 01-1 1H3.5l-2 2V3a1 1 0 011-1h7.5a1 1 0 011 1v4.5z" stroke="currentColor" strokeWidth="1.25" strokeLinejoin="round" />
            </svg>
            Notes
          </button>
          <ExportButton protocol={protocol} drug={drug} />
        </div>
      </header>

      {/* ── Body ───────────────────────────────────────────────────── */}
      <div className="flex flex-1 overflow-hidden">
        <SectionSidebar order={order} sections={sections} activeSection={activeSection} onScrollTo={scrollToSection} />

        <main ref={mainRef} onScroll={handleScroll} className="editor-canvas flex-1 overflow-y-auto">
          <div className="max-w-3xl mx-auto px-6 py-8">
            {order.map((name, idx) => (
              <SectionEditor
                key={`${name}-${contentVersions[name]}`}
                protocolId={protocol.id}
                sectionName={name}
                sectionTitle={SECTION_DISPLAY_NAMES[name as SectionName]}
                number={idx + 1}
                initialContent={sections[name]}
                onContentChange={handleContentChange}
                isRegenerating={regenerating === name}
                onRegenerate={() => handleRegenerate(name)}
                regenError={regenError[name]}
              />
            ))}
            <div className="h-32" />
          </div>
        </main>

        {commentsOpen && (
          <CommentPanel protocolId={protocol.id} activeSection={activeSection} />
        )}

        {qcOpen && (
          <QCPanel protocolId={protocol.id} onJumpTo={scrollToSection} />
        )}

        {coverageOpen && (
          <CoveragePanel protocolId={protocol.id} onJumpTo={scrollToSection} />
        )}

        {riskOpen && (
          <RiskPanel protocolId={protocol.id} onJumpTo={scrollToSection} />
        )}
      </div>

      <SourcesPanel sections={sections} open={sourcesOpen} onToggle={() => setSourcesOpen(o => !o)} />
    </div>
  )
}
