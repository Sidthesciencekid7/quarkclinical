import { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react'
import type { Drug, GeneratedProtocol, SectionName, StudyType } from '../types'
import { SECTION_DISPLAY_NAMES, visibleSections } from '../types'
import { api } from '../api/client'
import { ChatDrawer } from '../chat'
import { useCapabilities } from '../lib/useCapabilities'
import { STUDY_TYPE_LABELS } from '../shell/format'
import {
  AlertTriangle,
  Badge,
  Banner,
  Button,
  ChevronLeft,
  ClipboardList,
  IconButton,
  MessageSquare,
  PillNav,
  Shield,
  Sparkles,
} from '../ui'
import type { BadgeVariant } from '../ui'
import SectionEditor from './SectionEditor'
import SectionSidebar from './SectionSidebar'
import CommentPanel from './CommentPanel'
import SourcesPanel from './SourcesPanel'
import QCPanel from './QCPanel'
import CoveragePanel from './CoveragePanel'
import RiskPanel from './RiskPanel'
import ExportButton from './ExportButton'

/** Modality hint beside the study type (food-effect studies have none). */
const MODALITY_BADGE: Partial<Record<StudyType, string>> = {
  BE: 'Small molecule',
  biosimilar_pk: 'Biologic',
}

const STATUS_BADGE: Record<string, { label: string; variant: BadgeVariant }> = {
  draft: { label: 'Draft', variant: 'neutral' },
  review: { label: 'In review', variant: 'warning' },
  approved: { label: 'Approved', variant: 'success' },
}

type PanelId = 'qc' | 'coverage' | 'risk' | 'notes'

const PANEL_ITEMS = [
  { id: 'qc', label: 'QC', icon: <Shield aria-hidden /> },
  { id: 'coverage', label: 'Coverage', icon: <ClipboardList aria-hidden /> },
  { id: 'risk', label: 'Risk', icon: <AlertTriangle aria-hidden /> },
  { id: 'notes', label: 'Notes', icon: <MessageSquare aria-hidden /> },
]

interface Props {
  protocol: GeneratedProtocol
  drug: Drug
  onBack: () => void
  /**
   * Re-fetch the protocol (called after an "Edit with AI" change is applied).
   * The new `protocol` prop is merged in: sections whose server text changed are
   * remounted with the new content. A rejection shows a notice.
   */
  onReload?: () => Promise<void> | void
}

export default function ProtocolEditor({ protocol, drug, onBack, onReload }: Props) {
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
  const [panel, setPanel] = useState<PanelId | null>(null)
  const [sourcesOpen, setSourcesOpen] = useState(false)
  const [chatOpen, setChatOpen] = useState(false)
  const [reloadError, setReloadError] = useState<string | null>(null)
  const mainRef = useRef<HTMLDivElement>(null)
  const reasonId = useId()

  const { capabilities } = useCapabilities()

  // A re-fetched protocol (after an AI edit was applied) replaces the sections
  // whose server text changed; untouched sections keep any unsaved typing.
  const lastProtocol = useRef(protocol)
  useEffect(() => {
    const prev = lastProtocol.current
    if (prev === protocol) return
    lastProtocol.current = protocol
    const changed = order.filter(n => (prev.sections[n] ?? '') !== (protocol.sections[n] ?? ''))
    if (changed.length === 0) return
    setSections(s => ({ ...s, ...Object.fromEntries(changed.map(n => [n, protocol.sections[n] ?? ''])) }))
    setContentVersions(v => ({ ...v, ...Object.fromEntries(changed.map(n => [n, (v[n] ?? 0) + 1])) }))
  }, [protocol, order])

  const handleScroll = useCallback(() => {
    const scroller = mainRef.current
    // At the very bottom the last section can never reach the top edge, so
    // highlight it explicitly (otherwise "References" never lights up).
    if (scroller && scroller.scrollTop > 0 && scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 4) {
      setActiveSection(order[order.length - 1])
      return
    }
    const top = scroller?.getBoundingClientRect().top ?? 0
    for (let i = order.length - 1; i >= 0; i--) {
      const el = document.getElementById(`section-${order[i]}`)
      if (!el) continue
      if (el.getBoundingClientRect().top - top <= 100) {
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

  const handleApplied = useCallback(async () => {
    setReloadError(null)
    try {
      await onReload?.()
    } catch (e) {
      setReloadError(e instanceof Error ? e.message : 'Could not refresh the protocol.')
    }
  }, [onReload])

  const completedCount = order.filter(n => sections[n]?.trim()).length
  const status = STATUS_BADGE[protocol.status] ?? { label: protocol.status, variant: 'neutral' as BadgeVariant }
  const modality = MODALITY_BADGE[protocol.study_type]

  // "Edit with AI" is for drafts only, and only for roles that can generate.
  const editBlockedReason: string | null =
    protocol.status !== 'draft'
      ? protocol.status === 'approved'
        ? 'This protocol is signed and locked, so it can no longer be edited with AI.'
        : `Only draft protocols can be edited with AI (this one is ${status.label.toLowerCase()}).`
      : capabilities && !capabilities.can_generate
        ? 'Your role cannot edit protocols with AI. Ask an author on your team.'
        : null

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* ── Toolbar ────────────────────────────────────────────────── */}
      <header className="flex flex-none flex-wrap items-center gap-x-3 gap-y-2 border-b border-line bg-surface px-4 py-2.5">
        <IconButton label="Back to protocols" icon={<ChevronLeft />} size="sm" onClick={onBack} />

        <div className="flex min-w-0 flex-1 basis-56 flex-wrap items-center gap-x-2 gap-y-1">
          <h1 className="min-w-0 truncate text-h3 capitalize text-fg">{drug.inn}</h1>
          <Badge variant="neutral">{STUDY_TYPE_LABELS[protocol.study_type] ?? protocol.study_type}</Badge>
          {modality ? (
            <Badge variant="info" className="hidden sm:inline-flex">
              {modality}
            </Badge>
          ) : null}
          <Badge variant={status.variant} dot>
            {status.label}
          </Badge>
          <span className="text-small tabular-nums text-fg-subtle">
            v{protocol.version} · {completedCount}/{order.length} drafted
          </span>
        </div>

        <PillNav
          ariaLabel="Review panels"
          items={PANEL_ITEMS}
          value={panel ?? ''}
          onChange={id => setPanel(current => (current === id ? null : (id as PanelId)))}
        />

        <div className="flex flex-none items-center gap-2">
          <span title={editBlockedReason ?? undefined} className="inline-flex">
            <Button
              variant="secondary"
              leftIcon={<Sparkles aria-hidden />}
              disabled={editBlockedReason !== null}
              aria-describedby={editBlockedReason ? reasonId : undefined}
              onClick={() => setChatOpen(true)}
            >
              Edit with AI
            </Button>
          </span>
          {editBlockedReason ? (
            <span id={reasonId} className="sr-only">
              {editBlockedReason}
            </span>
          ) : null}
          <ExportButton protocol={protocol} drug={drug} />
        </div>
      </header>

      {reloadError ? (
        <div className="flex-none px-4 pt-3">
          <Banner tone="warning" title="The edit was applied, but this page could not refresh" onDismiss={() => setReloadError(null)}>
            {reloadError} Reload the page to see the new version.
          </Banner>
        </div>
      ) : null}

      {/* ── Body ───────────────────────────────────────────────────── */}
      <div className="relative flex min-h-0 flex-1">
        <SectionSidebar order={order} sections={sections} activeSection={activeSection} onScrollTo={scrollToSection} />

        <section
          ref={mainRef}
          aria-label="Protocol document"
          onScroll={handleScroll}
          className="editor-canvas min-w-0 flex-1 overflow-y-auto"
        >
          <div className="mx-auto max-w-3xl px-4 py-6 sm:px-6 sm:py-8">
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
        </section>

        {panel === 'notes' ? <CommentPanel protocolId={protocol.id} activeSection={activeSection} /> : null}
        {panel === 'qc' ? <QCPanel protocolId={protocol.id} onJumpTo={scrollToSection} /> : null}
        {panel === 'coverage' ? <CoveragePanel protocolId={protocol.id} onJumpTo={scrollToSection} /> : null}
        {panel === 'risk' ? <RiskPanel protocolId={protocol.id} onJumpTo={scrollToSection} /> : null}
      </div>

      <SourcesPanel sections={sections} open={sourcesOpen} onToggle={() => setSourcesOpen(o => !o)} />

      <ChatDrawer
        protocolId={protocol.id}
        open={chatOpen}
        onClose={() => setChatOpen(false)}
        onApplied={() => void handleApplied()}
      />
    </div>
  )
}
