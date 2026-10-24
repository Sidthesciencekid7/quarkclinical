import { useNavigate } from 'react-router-dom'
import ReportsView from '../../components/ReportsView'
import { newStudyHref } from '../../shell/config'

const noop = () => {}

/** `/app/protocols`: the org's protocol list (ReportsView), wired to URLs. */
export default function ProtocolsPage() {
  const navigate = useNavigate()
  return (
    <ReportsView
      onOpen={summary => navigate(`/app/protocols/${summary.id}`)}
      onOpenCsr={summary => navigate(`/app/protocols/${summary.id}/report`)}
      onNew={() => navigate(newStudyHref('protocol'))}
      openingId={null}
      openError={null}
      onDismissOpenError={noop}
    />
  )
}
