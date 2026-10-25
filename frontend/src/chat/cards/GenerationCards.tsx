import { useEffect, useRef } from 'react'
import { Link } from 'react-router-dom'
import { ReviewTable } from '../../components/protocol/ReviewTable'
import { GenerationProgress } from '../../components/protocol/GenerationProgress'
import { Button, ButtonLink, Calculator, FileText, Sparkles } from '../../ui'
import { useGenerationPoll } from '../../lib/useGenerationPoll'
import type { GenerationPhase } from '../../lib/useGenerationPoll'
import { guidelineLabel } from './labels'
import { studyTypeLabel } from '../../lib/studyTypes'
import type { Card, GenerationProgressCardData, ProtocolReadyCardData, ProtocolSummaryCardData } from '../types'
import { BulletList, Callout, CardFrame, SummaryTable } from './CardFrame'
import { useCardContext } from './CardContext'
import { capitalize, list } from './format'

/* --------------------------------------------------------- protocol_summary */

export function ProtocolSummaryCard({ card }: { card: Card }) {
  const d = card.data as unknown as ProtocolSummaryCardData
  const missing = list(d.missing)
  return (
    <CardFrame card={card} icon={<FileText />} doneLabel="Submitted">
      <SummaryTable rows={d.rows} ariaLabel="Protocol summary" />
      {!d.ready && missing.length > 0 ? (
        <div className="mt-3">
          <Callout tone="warning">
            <p className="font-semibold">Not ready to generate yet. Still needed:</p>
            <BulletList items={missing} className="mt-1 text-[color:inherit]" />
          </Callout>
        </div>
      ) : null}
      {d.blocking_issue ? (
        <div className="mt-3">
          <Callout tone="danger">{d.blocking_issue}</Callout>
        </div>
      ) : null}
      <BulletList items={d.notes} className="mt-3" />
    </CardFrame>
  )
}

/* ------------------------------------------------------- protocol_ready UI */

/** "Open in editor" and "Price this study": client navigations built from the protocol id. */
export function ProtocolReadyActions({ protocolId }: { protocolId: string }) {
  return (
    <>
      <ButtonLink as={Link} to={`/app/chat/new?kind=budget&protocol_id=${encodeURIComponent(protocolId)}`} variant="secondary" leftIcon={<Calculator />}>
        Price this study
      </ButtonLink>
      <ButtonLink as={Link} to={`/app/protocols/${encodeURIComponent(protocolId)}`} variant="primary" leftIcon={<FileText />}>
        Open in editor
      </ButtonLink>
    </>
  )
}

export function ProtocolReadyCard({ card }: { card: Card }) {
  const d = card.data as unknown as ProtocolReadyCardData
  const rows = [
    ...(d.drug_inn ? [{ label: 'Drug', value: capitalize(d.drug_inn) }] : []),
    ...(d.study_type ? [{ label: 'Study type', value: studyTypeLabel(d.study_type) }] : []),
    ...(d.guideline_version ? [{ label: 'Guideline', value: guidelineLabel(d.guideline_version) }] : []),
    { label: 'Sections', value: String(d.section_count ?? 0) },
    { label: 'Version', value: String(d.version ?? 1) },
  ]
  return (
    <CardFrame card={card} icon={<Sparkles />} doneLabel="Ready" footer={<ProtocolReadyActions protocolId={d.protocol_id} />} hideServerActions>
      <p className="mb-3 text-small text-fg-muted">
        The protocol is written and saved as a draft. Open it in the editor to review every section, run the QC checks and export it.
      </p>
      <ReviewTable ariaLabel="Protocol" rows={rows} />
    </CardFrame>
  )
}

/* ------------------------------------------------------- generation_progress */

export function GenerationProgressCard({ card }: { card: Card }) {
  const ctx = useCardContext()
  const d = card.data as unknown as GenerationProgressCardData
  const watching = card.status === 'active' && d.status === 'generating'
  const poll = useGenerationPoll(d.protocol_id, { enabled: watching })

  // When the watch ends, refetch the conversation: the server swaps this card for a
  // protocol_ready card (or marks it failed).
  const resyncedFor = useRef<string | null>(null)
  useEffect(() => {
    if (poll.phase !== 'complete' && poll.phase !== 'failed') return
    const key = `${d.protocol_id}:${poll.phase}`
    if (resyncedFor.current === key) return
    resyncedFor.current = key
    void ctx.resync()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [poll.phase, d.protocol_id])

  let phase: GenerationPhase
  let error: string | null = null
  if (d.status === 'failed' || card.status === 'error') {
    phase = 'failed'
    error = d.error
  } else if (d.status === 'complete') {
    phase = 'complete'
  } else if (!watching) {
    phase = 'running'
  } else {
    phase = poll.phase === 'idle' ? 'running' : poll.phase
    if (poll.phase === 'failed') error = poll.error
  }

  const hasReadyCard = ctx.cards.some(c => c.type === 'protocol_ready' && (c.data as { protocol_id?: string }).protocol_id === d.protocol_id)
  const showOpen = phase === 'complete' && !hasReadyCard
  const retryable = phase === 'failed'

  return (
    <CardFrame
      card={card}
      icon={<FileText />}
      title={d.drug_inn ? `Generating the ${capitalize(d.drug_inn)} protocol` : undefined}
      doneLabel="Complete"
      footer={
        showOpen ? (
          <ProtocolReadyActions protocolId={d.protocol_id} />
        ) : retryable ? (
          <Button variant="secondary" onClick={() => ctx.sendText('Show the summary')}>
            Review and try again
          </Button>
        ) : null
      }
      hideServerActions
    >
      <GenerationProgress phase={phase} elapsedMs={poll.elapsedMs} error={error} />
    </CardFrame>
  )
}
