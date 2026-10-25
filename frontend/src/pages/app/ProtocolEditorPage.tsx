import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { Banner, ButtonLink, ChevronLeft, Spinner } from '../../ui'
import ProtocolEditor from '../../editor/ProtocolEditor'
import { crumbKey, useSetCrumb } from '../../lib/breadcrumbs'
import { useActingUser } from '../../lib/actingUser'
import { loadProtocolBundle } from '../../shell/protocolLoader'
import type { ProtocolBundle } from '../../shell/protocolLoader'

type State =
  | { phase: 'loading' }
  | { phase: 'generating'; label: string | null }
  | { phase: 'error'; message: string; notFound: boolean }
  | { phase: 'ready'; bundle: ProtocolBundle }

const GENERATING_POLL_MS = 4000

/**
 * `/app/protocols/:id`: loads the protocol and opens it in the editor. A
 * protocol that is still being generated shows its status and opens by itself
 * when the pipeline finishes.
 */
export default function ProtocolEditorPage() {
  const { id = '' } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const { currentUser } = useActingUser()
  const [state, setState] = useState<State>({ phase: 'loading' })

  useEffect(() => {
    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | null = null
    setState({ phase: 'loading' })

    const load = async () => {
      try {
        const bundle = await loadProtocolBundle(id)
        if (cancelled) return
        const status = bundle.protocol.generation_status
        if (status === 'generating') {
          setState({ phase: 'generating', label: bundle.label })
          timer = setTimeout(load, GENERATING_POLL_MS)
          return
        }
        if (status === 'failed') {
          setState({
            phase: 'error',
            notFound: false,
            message: bundle.protocol.generation_error || 'Generating this protocol failed.',
          })
          return
        }
        setState({ phase: 'ready', bundle })
      } catch (e) {
        if (cancelled) return
        const message = e instanceof Error ? e.message : 'Could not open this protocol.'
        const status = (e as { status?: number } | null)?.status
        setState({ phase: 'error', message, notFound: status === 404 })
      }
    }
    void load()
    return () => {
      cancelled = true
      if (timer) clearTimeout(timer)
    }
    // Re-load when the acting user changes: a different org may not own this id.
  }, [id, currentUser?.id])

  // After "Edit with AI" applies a change: re-fetch quietly (no loading screen,
  // so the open drawer and scroll position survive) and hand the editor the new
  // protocol. A failure rejects so the editor can say so.
  const alive = useRef(true)
  useEffect(() => {
    alive.current = true
    return () => {
      alive.current = false
    }
  }, [])
  const reload = useCallback(async () => {
    const bundle = await loadProtocolBundle(id)
    if (!alive.current) return
    setState(prev => (prev.phase === 'ready' ? { phase: 'ready', bundle } : prev))
  }, [id])

  const label = state.phase === 'ready' ? state.bundle.label : state.phase === 'generating' ? state.label : null
  useSetCrumb(crumbKey.protocol(id), label)

  if (state.phase === 'loading') {
    return (
      <div className="flex min-h-[40vh] items-center justify-center gap-2 text-body text-fg-muted" role="status">
        <Spinner size={18} />
        <span>Opening protocol…</span>
      </div>
    )
  }

  if (state.phase === 'generating') {
    return (
      <div className="mx-auto max-w-2xl px-4 py-10">
        <Banner tone="info" title="This protocol is still being generated">
          <span className="inline-flex items-center gap-2">
            <Spinner size={14} />
            It will open here as soon as it is ready. You can leave this page; generation continues.
          </span>
        </Banner>
      </div>
    )
  }

  if (state.phase === 'error') {
    return (
      <div className="mx-auto max-w-2xl px-4 py-10">
        <Banner tone="danger" title={state.notFound ? 'Protocol not found' : 'Could not open this protocol'}>
          {state.message}
        </Banner>
        <div className="mt-4">
          <ButtonLink as={Link} to="/app/protocols" variant="secondary" leftIcon={<ChevronLeft />}>
            Back to protocols
          </ButtonLink>
        </div>
      </div>
    )
  }

  const { bundle } = state
  // The editor fills the shell's content area (h-full) and scrolls internally.
  return (
    <ProtocolEditor
      key={bundle.protocol.id}
      protocol={bundle.protocol}
      drug={bundle.drug}
      onBack={() => navigate('/app/protocols')}
      onReload={reload}
    />
  )
}
