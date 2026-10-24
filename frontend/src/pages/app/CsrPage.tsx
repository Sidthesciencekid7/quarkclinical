import { useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { Banner, ButtonLink, ChevronLeft, Spinner } from '../../ui'
import CSRView from '../../components/CSRView'
import { crumbKey, useSetCrumb } from '../../lib/breadcrumbs'
import { useActingUser } from '../../lib/actingUser'
import { loadProtocolBundle } from '../../shell/protocolLoader'
import type { ProtocolBundle } from '../../shell/protocolLoader'

/** `/app/protocols/:id/report`: the Clinical Study Report flow for one protocol. */
export default function CsrPage() {
  const { id = '' } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const { currentUser } = useActingUser()
  const [bundle, setBundle] = useState<ProtocolBundle | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setBundle(null)
    setError(null)
    loadProtocolBundle(id)
      .then(b => {
        if (!cancelled) setBundle(b)
      })
      .catch(e => {
        if (!cancelled) setError(e instanceof Error ? e.message : 'Could not open this protocol.')
      })
    return () => {
      cancelled = true
    }
  }, [id, currentUser?.id])

  useSetCrumb(crumbKey.protocol(id), bundle?.label)

  if (error) {
    return (
      <div className="mx-auto max-w-2xl px-4 py-10">
        <Banner tone="danger" title="Could not open the study report">
          {error}
        </Banner>
        <div className="mt-4">
          <ButtonLink as={Link} to="/app/protocols" variant="secondary" leftIcon={<ChevronLeft />}>
            Back to protocols
          </ButtonLink>
        </div>
      </div>
    )
  }

  if (!bundle) {
    return (
      <div className="flex min-h-[40vh] items-center justify-center gap-2 text-body text-fg-muted" role="status">
        <Spinner size={18} />
        <span>Opening study report…</span>
      </div>
    )
  }

  return <CSRView key={bundle.summary.id} protocol={bundle.summary} onBack={() => navigate('/app/protocols')} />
}
