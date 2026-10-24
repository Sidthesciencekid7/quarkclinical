import { useState, type RefObject } from 'react'
import { useNavigate } from 'react-router-dom'
import { Banner, Button, Chip, Composer, PillNav, type PillNavItem } from '../ui'
import { useLaunch } from '../lib/auth'
import {
  PENDING_PROMPT_MAX_CHARS,
  savePendingPrompt,
  type PendingPromptKind,
} from '../lib/pendingPrompt'
import { SUGGESTIONS } from './content'
import { launchTargetFor } from './handoff'
import { useIsSignedIn } from './useIsSignedIn'
import { FileText, Receipt, Sparkles } from './icons'

const MODES: PillNavItem[] = [
  { id: 'protocol', label: 'Protocol', icon: <FileText aria-hidden /> },
  { id: 'budget', label: 'Budget', icon: <Receipt aria-hidden /> },
]

const PLACEHOLDER: Record<PendingPromptKind, string> = {
  protocol: 'Describe your study, e.g. a biosimilar PK study of adalimumab in healthy volunteers',
  budget: 'Name a drug and describe the study to price, e.g. a patient study for nivolumab',
}

/**
 * The live composer in the hero (dark glass). It never talks to the backend:
 * on send it parks the description in this browser (savePendingPrompt) and
 * hands the visitor to sign-up (or straight into the app if they already have
 * a session). The text is never put in the URL - the launch target carries
 * only the fixed `kind` token.
 */
export function HeroComposer({ textareaRef }: { textareaRef?: RefObject<HTMLTextAreaElement> }) {
  const { signup } = useLaunch()
  const navigate = useNavigate()
  const signedIn = useIsSignedIn()
  const [text, setText] = useState('')
  const [mode, setMode] = useState<PendingPromptKind>('protocol')
  const [storageBlocked, setStorageBlocked] = useState(false)

  const proceed = (kind: PendingPromptKind) => {
    const target = launchTargetFor(kind)
    if (signedIn) navigate(target)
    else signup(target)
  }

  const handleSend = (raw: string) => {
    if (!savePendingPrompt({ text: raw, kind: mode })) {
      // Private window / blocked site data: the text cannot be carried across
      // sign-up. Say so instead of silently dropping it.
      setStorageBlocked(true)
      return
    }
    setStorageBlocked(false)
    proceed(mode)
  }

  const applySuggestion = (s: (typeof SUGGESTIONS)[number]) => {
    setText(s.text)
    setMode(s.kind)
    setStorageBlocked(false)
    textareaRef?.current?.focus()
  }

  return (
    <div className="mx-auto mt-10 w-full">
      <div
        role="group"
        aria-label="Example studies"
        className="mx-auto flex max-w-4xl flex-wrap justify-center gap-2"
      >
        {SUGGESTIONS.map(s => (
          <Chip
            key={s.text}
            icon={<Sparkles aria-hidden />}
            onClick={() => applySuggestion(s)}
            className="h-auto min-h-8 max-w-full whitespace-normal py-1.5 text-left"
          >
            {s.text}
          </Chip>
        ))}
      </div>

      <div className="mx-auto mt-4 max-w-2xl text-left">
        <Composer
          ref={textareaRef}
          surface="dark"
          value={text}
          onChange={v => {
            setText(v)
            if (storageBlocked) setStorageBlocked(false)
          }}
          onSend={handleSend}
          placeholder={PLACEHOLDER[mode]}
          textareaLabel="Describe your study"
          sendLabel="Start with this study"
          maxLength={PENDING_PROMPT_MAX_CHARS}
          minRows={2}
          maxRows={6}
          footerLeft={
            <PillNav
              ariaLabel="What to start"
              size="sm"
              items={MODES}
              value={mode}
              onChange={id => setMode(id === 'budget' ? 'budget' : 'protocol')}
            />
          }
        />

        {storageBlocked ? (
          <Banner
            tone="warning"
            className="mt-3"
            action={
              <Button variant="secondary" size="sm" onClick={() => proceed(mode)}>
                Continue anyway
              </Button>
            }
          >
            Your browser is blocking local storage, so your description cannot be carried into the app. You can
            continue and retype it there.
          </Banner>
        ) : null}

        <p className="mt-3 text-center text-small text-fg-subtle">
          Nothing is sent to AI until you sign in. Your description stays in this browser.
        </p>
      </div>
    </div>
  )
}
