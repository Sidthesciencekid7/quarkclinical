/**
 * Watches an edit proposal (`GET /api/chat/{sid}/proposals/{pid}`) while it is
 * being prepared, the same way `useGenerationPoll` watches a protocol: 3 s
 * interval, transient errors tolerated, ~30 minutes at most, and running out of
 * patience is reported as "still running", never as a failure. The server marks
 * a proposal stuck for more than 30 minutes as failed when it is read.
 */
import { useEffect, useRef, useState } from 'react'
import { getProposal } from '../../api/chat'
import { abortableSleep, isAbortError } from '../../lib/useGenerationPoll'
import type { ProposalRead, ProposalStatus } from '../types'

export const PROPOSAL_POLL_INTERVAL_MS = 3000
export const PROPOSAL_POLL_MAX_ATTEMPTS = 600

export type ProposalPhase = 'idle' | 'running' | 'finished' | 'still_running'

export interface ProposalPollState {
  phase: ProposalPhase
  /** The last proposal the server returned (null until the first poll succeeds). */
  proposal: ProposalRead | null
  status: ProposalStatus | null
}

const IDLE: ProposalPollState = { phase: 'idle', proposal: null, status: null }
const ACTIVE: ReadonlySet<ProposalStatus> = new Set<ProposalStatus>(['proposed', 'generating'])

export interface ProposalPollOptions {
  enabled?: boolean
  intervalMs?: number
  maxAttempts?: number
  /** Injectable for tests. */
  fetchProposal?: (sessionId: string, proposalId: string, opts: { signal?: AbortSignal }) => Promise<ProposalRead>
  sleep?: (ms: number, signal?: AbortSignal) => Promise<void>
}

export function useProposalPoll(sessionId: string | null | undefined, proposalId: string | null | undefined, options: ProposalPollOptions = {}): ProposalPollState {
  const { enabled = true } = options
  const [state, setState] = useState<ProposalPollState>(IDLE)
  const optsRef = useRef(options)
  optsRef.current = options

  useEffect(() => {
    if (!sessionId || !proposalId || !enabled) {
      setState(IDLE)
      return
    }
    const controller = new AbortController()
    const { intervalMs = PROPOSAL_POLL_INTERVAL_MS, maxAttempts = PROPOSAL_POLL_MAX_ATTEMPTS, fetchProposal = getProposal, sleep = abortableSleep } = optsRef.current
    setState({ phase: 'running', proposal: null, status: null })

    void (async () => {
      for (let attempt = 0; attempt < maxAttempts; attempt++) {
        try {
          if (attempt > 0) await sleep(intervalMs, controller.signal)
          if (controller.signal.aborted) return
          let p: ProposalRead
          try {
            p = await fetchProposal(sessionId, proposalId, { signal: controller.signal })
          } catch (e) {
            if (controller.signal.aborted || isAbortError(e)) return
            continue // transient blip: the job is still running server-side
          }
          if (controller.signal.aborted) return
          if (!ACTIVE.has(p.status)) {
            setState({ phase: 'finished', proposal: p, status: p.status })
            return
          }
          setState({ phase: 'running', proposal: p, status: p.status })
        } catch (e) {
          if (isAbortError(e)) return
        }
      }
      if (!controller.signal.aborted) setState(s => ({ ...s, phase: 'still_running' }))
    })()

    return () => controller.abort()
  }, [sessionId, proposalId, enabled])

  return state
}
