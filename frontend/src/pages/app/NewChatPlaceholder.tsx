import { useMemo } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { Card, ButtonLink, Calculator, FileText, Sparkles } from '../../ui'
import { loadPendingPrompt } from '../../lib/pendingPrompt'
import { BUDGET_WIZARD_PATH, PROTOCOL_WIZARD_PATH } from '../../shell/config'
import { loadHomeDraft } from '../../shell/homeDraft'

const PRESET_LABELS: Record<string, string> = {
  biosimilar_pk: 'Biosimilar PK protocol',
  be: 'Bioequivalence protocol',
  food_effect: 'Food-effect protocol',
  price: 'Price a study',
}

/**
 * Stand-in for `/app/chat/new` and `/app/chat/:sessionId` until the chat UI
 * ships (the backend chat routes do not exist yet). It keeps the destination of
 * every "set up a study" link valid, shows any description the user already
 * typed (Home composer: sessionStorage `qc_home_draft`; landing page:
 * localStorage `qc_pending_prompt_v1`), and sends them to the classic wizards.
 * Reads `?kind=&preset=&from=`; nothing is sent to a server from here.
 */
export default function NewChatPlaceholder() {
  const { sessionId } = useParams<{ sessionId?: string }>()
  const [params] = useSearchParams()
  const kind = params.get('kind') === 'budget' ? 'budget' : 'protocol'
  const preset = params.get('preset')
  const from = params.get('from')

  const draft = useMemo(() => {
    const home = loadHomeDraft()
    const pending = loadPendingPrompt()
    if (from === 'landing') return pending ? { text: pending.text, source: 'landing' as const } : null
    if (home) return { text: home.text, source: 'home' as const }
    return pending ? { text: pending.text, source: 'landing' as const } : null
  }, [from])

  const isSession = !!sessionId

  return (
    <div className="mx-auto w-full max-w-2xl px-4 py-10 sm:px-8 sm:py-14">
      <span
        aria-hidden
        className="mb-5 flex h-12 w-12 items-center justify-center rounded-tile bg-surface-muted text-fg-muted [&>svg]:h-5 [&>svg]:w-5"
      >
        <Sparkles />
      </span>
      <h1 className="text-h1 text-fg">{isSession ? 'This conversation is not available yet' : 'Chat is coming'}</h1>
      <p className="mt-2 text-body text-fg-muted">
        {isSession
          ? 'Conversations will open here once study chat is live. Your protocols and budgets are unaffected.'
          : 'Setting up a study by describing it in plain language is on its way. Until then, the step-by-step tools below do the same job and use the same engines.'}
      </p>

      {preset && PRESET_LABELS[preset] ? (
        <p className="mt-4 text-small text-fg-subtle">
          You picked <span className="font-medium text-fg-muted">{PRESET_LABELS[preset]}</span>.
        </p>
      ) : null}

      {draft && !isSession ? (
        <Card className="mt-6" aria-labelledby="draft-saved-title">
          <h2 id="draft-saved-title" className="text-h3 text-fg">
            Your study description is saved
          </h2>
          <p className="mt-1 text-small text-fg-muted">
            {draft.source === 'landing'
              ? 'You wrote this on the QuarkClinical home page. It stays in this browser, and nothing has been sent to AI.'
              : 'It stays in this tab, and nothing has been sent to AI.'}
          </p>
          <blockquote className="mt-3 max-h-48 overflow-y-auto whitespace-pre-wrap rounded-xl bg-surface-muted px-4 py-3 text-body text-fg">
            {draft.text}
          </blockquote>
        </Card>
      ) : null}

      <div className="mt-8 flex flex-wrap items-center gap-3">
        <ButtonLink
          as={Link}
          to={kind === 'budget' ? BUDGET_WIZARD_PATH : PROTOCOL_WIZARD_PATH}
          variant="primary"
          size="lg"
          leftIcon={kind === 'budget' ? <Calculator /> : <FileText />}
        >
          {kind === 'budget' ? 'Open the budget tool' : 'Open the protocol wizard'}
        </ButtonLink>
        <ButtonLink
          as={Link}
          to={kind === 'budget' ? PROTOCOL_WIZARD_PATH : BUDGET_WIZARD_PATH}
          variant="secondary"
          size="lg"
          leftIcon={kind === 'budget' ? <FileText /> : <Calculator />}
        >
          {kind === 'budget' ? 'Open the protocol wizard' : 'Open the budget tool'}
        </ButtonLink>
        <ButtonLink as={Link} to="/app" variant="ghost" size="lg">
          Back to Home
        </ButtonLink>
      </div>
    </div>
  )
}
