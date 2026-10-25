import { useEffect, useRef, useState } from 'react'
import { ReviewTable } from '../../components/protocol/ReviewTable'
import { AlertTriangle, Badge, Button, ClipboardList, FileText, Pencil, RefreshCw, Spinner, cx } from '../../ui'
import type { BadgeVariant } from '../../ui'
import { studyTypeLabel } from '../../lib/studyTypes'
import type {
  Card,
  EditDiffCardData,
  EditImpactCardData,
  EditProgressCardData,
  FactRow,
  ProposalRead,
  ProtocolOverviewCardData,
  SectionDiffItem,
} from '../types'
import { BulletList, Callout, CardFrame, useCardInert } from './CardFrame'
import { useCardContext } from './CardContext'
import { capitalize, list } from './format'
import { guidelineLabel } from './labels'
import { useProposalPoll } from './useProposalPoll'

/* ------------------------------------------------------ protocol_overview */

export function ProtocolOverviewCard({ card }: { card: Card }) {
  const d = card.data as unknown as ProtocolOverviewCardData
  const rows = [
    { label: 'Status', value: capitalize(d.status ?? '') },
    { label: 'Version', value: String(d.version ?? '') },
    { label: 'Study type', value: studyTypeLabel(d.study_type) },
    { label: 'Guideline', value: guidelineLabel(d.guideline_version) },
    ...(d.generation_status && d.generation_status !== 'complete' ? [{ label: 'Generation', value: capitalize(d.generation_status) }] : []),
  ]
  const sections = list(d.sections)
  const facts = list(d.locked_facts)
  return (
    <CardFrame card={card} icon={<FileText />} title={d.drug_inn ? `${capitalize(d.drug_inn)} protocol` : undefined}>
      {!d.editable ? (
        <div className="mb-3">
          <Callout tone="warning">{d.blocked_reason ?? 'This protocol cannot be edited in chat.'}</Callout>
        </div>
      ) : null}
      <ReviewTable ariaLabel="Protocol" rows={rows} />
      {facts.length > 0 ? (
        <>
          <p className="mb-1.5 mt-4 text-eyebrow uppercase text-fg-subtle">Locked by the engines</p>
          <ReviewTable ariaLabel="Facts locked by the engines" rows={facts.map(f => ({ label: f.label, value: f.value }))} />
        </>
      ) : null}
      {sections.length > 0 ? (
        <details className="mt-4 text-small text-fg-muted">
          <summary className="cursor-pointer select-none font-medium hover:text-fg">Sections ({sections.length})</summary>
          <ul className="m-0 mt-2 list-none divide-y divide-line overflow-hidden rounded-input border border-line p-0">
            {sections.map(s => (
              <li key={s.name} className="flex items-baseline justify-between gap-3 px-3.5 py-2">
                <span className="min-w-0 truncate text-fg">{s.label}</span>
                <span className="flex-none tabular-nums text-fg-subtle">{s.words} words</span>
              </li>
            ))}
          </ul>
        </details>
      ) : null}
    </CardFrame>
  )
}

/* ---------------------------------------------------------------- edit_impact */

const DEFAULT_RERUN =
  'Changing an engine input re-runs the full generation for this protocol (up to about half an hour, and it uses generation credits). Nothing changes in the protocol until you review the result and apply it.'

export function EditImpactCard({ card }: { card: Card }) {
  const d = card.data as unknown as EditImpactCardData
  const changes = list(d.changes)
  const sections = list(d.sections)
  const estimate = d.estimate
  const engine = d.kind === 'engine'
  return (
    <CardFrame
      card={card}
      icon={<Pencil />}
      doneLabel="Started"
      title={engine ? 'Review the input change' : 'Review the rewrite'}
    >
      <p className="mb-3 flex flex-wrap items-center gap-2">
        <Badge variant={engine ? 'warning' : 'info'}>{engine ? 'Engine input change' : 'Section rewrite'}</Badge>
        <span className="text-small text-fg-subtle">Based on protocol version {d.base_version}</span>
      </p>

      {!d.editable ? (
        <div className="mb-3">
          <Callout tone="warning">{d.blocked_reason ?? 'This protocol cannot be edited in chat.'}</Callout>
        </div>
      ) : null}

      {engine && changes.length > 0 ? (
        <ul className="m-0 list-none divide-y divide-line overflow-hidden rounded-input border border-line p-0">
          {changes.map(c => (
            <li key={c.field} className="px-3.5 py-2.5 text-small">
              <p className="font-medium text-fg">{c.label}</p>
              <p className="mt-0.5 text-fg-muted">
                <span className="break-words">{c.before ?? 'not set'}</span>
                <span aria-hidden className="mx-1.5 text-fg-subtle">
                  {'→'}
                </span>
                <span className="sr-only"> changes to </span>
                <span className="break-words font-semibold text-fg">{c.after ?? 'not set'}</span>
              </p>
            </li>
          ))}
        </ul>
      ) : null}

      {!engine ? (
        <>
          {sections.length > 0 ? (
            <p className="mb-2 text-small text-fg-muted">
              Sections to rewrite:{' '}
              <span className="font-semibold text-fg">
                {sections.map(s => d.section_labels?.[s] ?? s.replace(/_/g, ' ')).join(', ')}
              </span>
            </p>
          ) : null}
          {d.instruction ? (
            <blockquote className="m-0 whitespace-pre-wrap break-words rounded-input border-l-4 border-line-strong bg-surface-muted px-4 py-2.5 text-small text-fg">
              {d.instruction}
            </blockquote>
          ) : null}
        </>
      ) : null}

      {estimate ? (
        <div className="mt-3">
          <p className="mb-1.5 flex flex-wrap items-center gap-2 text-eyebrow uppercase text-fg-subtle">
            {estimate.label || 'Quick estimate'}
            <Badge variant="neutral">Estimate</Badge>
          </p>
          <ReviewTable ariaLabel="Estimate" rows={list(estimate.rows).map(r => ({ label: r.label, value: r.value }))} />
          <p className="mt-1.5 text-small text-fg-subtle">
            {estimate.note ? `${estimate.note} ` : ''}Estimate only; the final values come from the full re-run.
          </p>
        </div>
      ) : null}

      {list(d.warnings).length > 0 ? (
        <div className="mt-3">
          <Callout tone="warning">
            <BulletList items={d.warnings} className="text-[color:inherit]" />
          </Callout>
        </div>
      ) : null}

      <div className="mt-3">
        <Callout tone={engine ? 'warning' : 'info'}>
          {engine ? (
            <>
              <span className="font-semibold">Re-runs generation and uses credits.</span> {d.rerun_note || DEFAULT_RERUN}
            </>
          ) : (
            d.rerun_note
          )}
        </Callout>
      </div>
    </CardFrame>
  )
}

/* --------------------------------------------------------------- diff view */

const SECTION_BADGE: Record<SectionDiffItem['status'], { label: string; variant: BadgeVariant }> = {
  unchanged: { label: 'Unchanged', variant: 'neutral' },
  modified: { label: 'Changed', variant: 'warning' },
  added: { label: 'Added', variant: 'success' },
  removed: { label: 'Removed', variant: 'danger' },
}

const MAX_DIFF_LINES = 120

/** A unified-diff line as prose: file/hunk headers dropped, the +/-/space marker
 *  turned into a change kind so reviewers read sentences, not diff syntax. */
function parseDiffLines(lines: string[]): { kind: 'added' | 'removed' | 'same'; text: string }[] {
  return lines
    .filter(l => !l.startsWith('+++') && !l.startsWith('---') && !l.startsWith('@@'))
    .map(l => {
      const kind = l.startsWith('+') ? 'added' : l.startsWith('-') ? 'removed' : 'same'
      return { kind, text: kind === 'same' && !l.startsWith(' ') ? l : l.slice(1) }
    })
}

function DiffLines({ lines }: { lines: string[] }) {
  const [all, setAll] = useState(false)
  const shown = parseDiffLines(all ? lines : lines.slice(0, MAX_DIFF_LINES))
  return (
    <>
      <div className="max-h-96 overflow-auto rounded-input border border-line bg-surface-muted p-3 text-small leading-6">
        {shown.map((line, i) =>
          line.text.trim() === '' ? (
            <div key={i} className="h-2" aria-hidden />
          ) : (
            <p
              key={i}
              className={cx(
                'm-0 whitespace-pre-wrap break-words border-l-2 px-2.5',
                line.kind === 'added' && 'border-[color:var(--ok-fg)] bg-[var(--ok-bg)] text-[color:var(--ok-fg)]',
                line.kind === 'removed' && 'border-[color:var(--bad-fg)] bg-[var(--bad-bg)] text-[color:var(--bad-fg)] line-through decoration-1',
                line.kind === 'same' && 'border-transparent text-fg-muted',
              )}
            >
              {line.kind !== 'same' ? <span className="sr-only">{line.kind === 'added' ? 'Added: ' : 'Removed: '}</span> : null}
              {line.text}
            </p>
          ),
        )}
      </div>
      {lines.length > MAX_DIFF_LINES && !all ? (
        <Button variant="link" size="sm" onClick={() => setAll(true)}>
          Show all {lines.length} lines
        </Button>
      ) : null}
    </>
  )
}

function wordDelta(n: number): string {
  if (n === 0) return 'same length'
  return `${n > 0 ? '+' : '−'}${Math.abs(n)} words`
}

/** Turn the server's facts (a FactRow list, or, from the proposal route, an object) into rows. */
export function factRows(facts: unknown): FactRow[] {
  if (Array.isArray(facts)) {
    return facts.flatMap(f => (f && typeof f === 'object' && 'label' in f ? [{ label: String((f as FactRow).label), value: String((f as FactRow).value ?? '') }] : []))
  }
  if (facts && typeof facts === 'object') {
    return Object.entries(facts as Record<string, unknown>).map(([k, v]) => ({ label: k, value: typeof v === 'string' ? v : JSON.stringify(v) }))
  }
  return []
}

interface QcFinding {
  title: string
  status: string
  detail: string
}

/** The deterministic QC result, read defensively (its shape belongs to the QC engine). */
export function qcFindings(qc: unknown): { findings: QcFinding[]; total: number | null; submissionReady: boolean | null } {
  if (!qc || typeof qc !== 'object') return { findings: [], total: null, submissionReady: null }
  const q = qc as Record<string, unknown>
  const raw = Array.isArray(q.findings) ? q.findings : []
  const findings = raw.flatMap(f => {
    if (!f || typeof f !== 'object') return []
    const r = f as Record<string, unknown>
    const status = String(r.status ?? '')
    if (status === 'pass' || status === 'not_applicable') return []
    return [{ title: String(r.title ?? r.id ?? 'Check'), status, detail: String(r.detail ?? '') }]
  })
  return {
    findings,
    total: typeof q.total_checks === 'number' ? q.total_checks : null,
    submissionReady: typeof q.submission_ready === 'boolean' ? q.submission_ready : null,
  }
}

export interface EditDiffViewProps {
  sections: SectionDiffItem[]
  qc: unknown
  factsBefore: FactRow[]
  factsAfter: FactRow[]
  stale?: boolean
  appliedVersion?: number | null
  status?: string
  onRerun?: () => void
  rerunDisabled?: boolean
}

export function EditDiffView({ sections, qc, factsBefore, factsAfter, stale, appliedVersion, status, onRerun, rerunDisabled }: EditDiffViewProps) {
  const changed = sections.filter(s => s.status !== 'unchanged')
  const unchanged = sections.length - changed.length
  const after = new Map(factsAfter.map(f => [f.label, f.value]))
  const before = new Map(factsBefore.map(f => [f.label, f.value]))
  const labels = [...new Set([...factsBefore.map(f => f.label), ...factsAfter.map(f => f.label)])]
  const changedFacts = labels.filter(l => before.get(l) !== after.get(l))
  const qcInfo = qcFindings(qc)

  return (
    <div className="space-y-3">
      {status === 'applied' || appliedVersion ? (
        <Callout tone="info">Applied{appliedVersion ? `: the protocol is now version ${appliedVersion}.` : '.'}</Callout>
      ) : null}
      {status === 'discarded' ? <Callout tone="neutral">Discarded. Nothing was changed.</Callout> : null}
      {stale ? (
        <Callout tone="warning">
          <p>The protocol changed after this edit was prepared, so it can no longer be applied.</p>
          {onRerun ? (
            <Button className="mt-2" size="sm" variant="secondary" leftIcon={<RefreshCw />} disabled={rerunDisabled} onClick={onRerun}>
              Re-run on the current version
            </Button>
          ) : null}
        </Callout>
      ) : null}

      {changed.length === 0 ? <p className="text-small text-fg-muted">No section text changed.</p> : null}
      {changed.map((s, i) => {
        const b = SECTION_BADGE[s.status] ?? SECTION_BADGE.modified
        return (
          <details key={s.section_name} open={i === 0} className="rounded-input border border-line">
            <summary className="flex cursor-pointer select-none flex-wrap items-center gap-2 px-3.5 py-2.5 text-small">
              <span className="font-semibold text-fg">{s.display_name}</span>
              <Badge variant={b.variant}>{b.label}</Badge>
              <span className="text-fg-subtle">{wordDelta(s.word_count_delta ?? 0)}</span>
            </summary>
            <div className="border-t border-line p-3">
              {list(s.diff_lines).length > 0 ? <DiffLines lines={s.diff_lines} /> : <p className="text-small text-fg-muted">No line-level differences to show.</p>}
            </div>
          </details>
        )
      })}
      {unchanged > 0 ? <p className="text-small text-fg-subtle">{unchanged} other section{unchanged === 1 ? '' : 's'} unchanged.</p> : null}

      {changedFacts.length > 0 ? (
        <div>
          <p className="mb-1.5 text-eyebrow uppercase text-fg-subtle">Locked facts that changed</p>
          <div className="overflow-x-auto rounded-input border border-line">
            <table className="w-full min-w-[22rem] text-left text-small">
              <thead className="bg-surface-muted text-fg-subtle">
                <tr>
                  <th scope="col" className="px-3 py-2 font-medium">Fact</th>
                  <th scope="col" className="px-3 py-2 font-medium">Before</th>
                  <th scope="col" className="px-3 py-2 font-medium">After</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line">
                {changedFacts.map(l => (
                  <tr key={l}>
                    <th scope="row" className="px-3 py-2 font-medium text-fg">{l}</th>
                    <td className="px-3 py-2 text-fg-muted">{before.get(l) ?? 'not set'}</td>
                    <td className="px-3 py-2 font-semibold text-fg">{after.get(l) ?? 'not set'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="mt-1.5 text-small text-fg-subtle">
            An engine edit re-runs today&apos;s engines, so this can include changes made to the engines since the protocol was first generated.
          </p>
        </div>
      ) : null}

      {qcInfo.findings.length > 0 ? (
        <div>
          <p className="mb-1.5 text-eyebrow uppercase text-fg-subtle">QC findings on the proposed text</p>
          <ul className="m-0 list-none space-y-1.5 p-0">
            {qcInfo.findings.slice(0, 8).map((f, i) => (
              <li key={i} className="flex items-start gap-2 text-small">
                <AlertTriangle aria-hidden className="mt-0.5 h-4 w-4 flex-none text-[color:var(--warn-fg)]" />
                <span className="min-w-0 break-words">
                  <span className="font-medium text-fg">{f.title}</span>
                  {f.detail ? <span className="text-fg-muted">: {f.detail}</span> : null}
                </span>
              </li>
            ))}
          </ul>
          {qcInfo.findings.length > 8 ? <p className="mt-1 text-small text-fg-subtle">and {qcInfo.findings.length - 8} more in the editor&apos;s QC panel.</p> : null}
        </div>
      ) : qcInfo.total !== null ? (
        <p className="text-small text-fg-subtle">QC: no findings on the proposed text.</p>
      ) : null}
    </div>
  )
}

/* ------------------------------------------------------------- edit_diff */

export function EditDiffCard({ card }: { card: Card }) {
  const ctx = useCardContext()
  const d = card.data as unknown as EditDiffCardData
  const inert = useCardInert(card)
  return (
    <CardFrame card={card} icon={<ClipboardList />} doneLabel={d.status === 'applied' ? 'Applied' : 'Done'}>
      <EditDiffView
        sections={list(d.sections)}
        qc={d.qc}
        factsBefore={list(d.facts_before)}
        factsAfter={list(d.facts_after)}
        stale={d.stale}
        appliedVersion={d.applied_version}
        status={d.status}
        onRerun={() => ctx.sendText('Re-run that edit on the current version')}
        rerunDisabled={inert}
      />
    </CardFrame>
  )
}

/* ---------------------------------------------------------- edit_progress */

export function EditProgressCard({ card }: { card: Card }) {
  const ctx = useCardContext()
  const d = card.data as unknown as EditProgressCardData
  const watching = card.status === 'active' && (d.status === 'generating' || d.status === 'proposed')
  const poll = useProposalPoll(ctx.sessionId, d.proposal_id, { enabled: watching })
  const inert = useCardInert(card)

  // When the proposal finishes, refetch the conversation: the server puts the
  // edit_diff card (or the failure) into it.
  const resyncedFor = useRef<string | null>(null)
  useEffect(() => {
    if (poll.phase !== 'finished') return
    const key = `${d.proposal_id}:${poll.status}`
    if (resyncedFor.current === key) return
    resyncedFor.current = key
    void ctx.resync()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [poll.phase, poll.status, d.proposal_id])

  const status = poll.phase === 'finished' && poll.status ? poll.status : d.status
  const proposal: ProposalRead | null = poll.proposal
  const hasDiffCard = ctx.cards.some(c => c.type === 'edit_diff' && (c.data as { proposal_id?: string }).proposal_id === d.proposal_id)
  const failed = status === 'failed'
  const stale = status === 'stale'
  const ready = status === 'ready' || status === 'applied'
  const engine = d.kind === 'engine'
  const fallbackDiff = ready && !!proposal && !hasDiffCard
  const applying = ctx.pending?.cardId === card.id && ctx.pending.action === 'apply_edit'
  const applyDisabled = ctx.busy || status !== 'ready'

  return (
    <CardFrame
      card={card}
      icon={<Pencil />}
      doneLabel={ready ? 'Ready' : 'Done'}
      hideServerActions
      footer={
        fallbackDiff && status === 'ready' ? (
          <>
            <Button variant="ghost" disabled={ctx.busy} onClick={() => void ctx.dispatch(card, 'discard_edit', { proposal_id: d.proposal_id })}>
              Discard
            </Button>
            <Button variant="primary" loading={applying} disabled={applyDisabled} onClick={() => void ctx.dispatch(card, 'apply_edit', { proposal_id: d.proposal_id })}>
              Apply to protocol
            </Button>
          </>
        ) : null
      }
    >
      {failed ? (
        <Callout tone="danger">
          <span className="font-semibold">The edit could not be prepared.</span> {proposal?.error ?? d.error ?? 'Nothing was changed.'}
        </Callout>
      ) : stale ? (
        <Callout tone="warning">
          The protocol changed while this edit was being prepared, so it was not applied.
          <div className="mt-2">
            <Button size="sm" variant="secondary" leftIcon={<RefreshCw />} disabled={inert} onClick={() => ctx.sendText('Re-run that edit on the current version')}>
              Re-run
            </Button>
          </div>
        </Callout>
      ) : ready ? (
        <p className="text-small text-fg-muted">The edit is ready. Review the differences below before applying anything.</p>
      ) : (
        <div role="status" className="flex items-start gap-3">
          <Spinner size={18} className="mt-0.5 text-accent-600" />
          <div className="text-small text-fg-muted">
            <p className="font-medium text-fg">Preparing the edit&hellip;</p>
            <p>
              {engine
                ? 'Re-running the full generation with your change. This can take up to about half an hour; you can leave this page and come back.'
                : 'Rewriting the sections you named. This usually takes a minute or two.'}
            </p>
            {poll.phase === 'still_running' ? (
              <p className="mt-1 text-[color:var(--warn-fg)]">Still working. Reopen this conversation later to see the result.</p>
            ) : null}
          </div>
        </div>
      )}

      {/* Fallback: the proposal is ready but the server has not put a diff card into the conversation. */}
      {fallbackDiff && proposal ? (
        <div className="mt-4">
          <EditDiffView
            sections={list(proposal.diff)}
            qc={proposal.qc}
            factsBefore={factRows(proposal.facts_before)}
            factsAfter={factRows(proposal.facts_after)}
            status={proposal.status}
            appliedVersion={proposal.applied_version}
          />
        </div>
      ) : null}
    </CardFrame>
  )
}
