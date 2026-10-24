import { useNavigate } from 'react-router-dom'
import BudgetList from '../../components/BudgetList'
import { newStudyHref } from '../../shell/config'

const noop = () => {}

/** `/app/budgets`: the org's budget list (BudgetList), wired to URLs. */
export default function BudgetsPage() {
  const navigate = useNavigate()
  return (
    <BudgetList
      onOpen={summary => navigate(`/app/budgets/${summary.id}`)}
      onNew={() => navigate(newStudyHref('budget'))}
      openingId={null}
      openError={null}
      onDismissOpenError={noop}
    />
  )
}
