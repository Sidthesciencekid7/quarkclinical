import { useId, useState, type FormEvent } from 'react'
import { api } from '../api/client'
import { Banner, Button, Chip, Field, Input, Modal, Textarea } from '../ui'

const INTEREST_OPTIONS = ['Book a demo', 'Request a pilot on a real study', 'Pricing and plans']

/**
 * "Book a demo" / contact-sales dialog, built on the design-system Modal
 * (focus trap, Esc to close, focus returns to the opener). Submitting posts a
 * lead to the public /api/leads route. The leads table has no interest
 * column, so the chosen interest is prefixed onto the message
 * ("[Book a demo] ...") so the operator sees what the prospect asked for.
 */
export default function ContactSalesModal({
  onClose,
  initialInterest = INTEREST_OPTIONS[0],
}: {
  onClose: () => void
  initialInterest?: string
}) {
  const formId = useId()
  const [interest, setInterest] = useState(initialInterest)
  const [companyName, setCompanyName] = useState('')
  const [contactName, setContactName] = useState('')
  const [contactEmail, setContactEmail] = useState('')
  const [teamSize, setTeamSize] = useState('')
  const [message, setMessage] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [sent, setSent] = useState(false)

  const canSubmit = Boolean(companyName.trim() && contactName.trim() && contactEmail.trim())

  const handleSubmit = async (e?: FormEvent) => {
    e?.preventDefault()
    if (!canSubmit || submitting) return
    setSubmitting(true)
    setError(null)
    try {
      await api.submitLead({
        company_name: companyName.trim(),
        contact_name: contactName.trim(),
        contact_email: contactEmail.trim(),
        team_size_estimate: teamSize.trim() || undefined,
        message: `[${interest}]${message.trim() ? ' ' + message.trim() : ''}`,
      })
      setSent(true)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not send your request — please try again.')
    } finally {
      setSubmitting(false)
    }
  }

  if (sent) {
    return (
      <Modal
        open
        onClose={onClose}
        size="sm"
        title="Thanks, we’ll be in touch"
        description="We’ve received your request and will reply by email to arrange a time."
        footer={
          <Button variant="primary" size="md" onClick={onClose} autoFocus>
            Close
          </Button>
        }
      />
    )
  }

  return (
    <Modal
      open
      onClose={onClose}
      size="md"
      title="Book a demo"
      description="See QuarkClinical on a study like yours. We’ll walk through a protocol and budget for a drug you choose, or scope a pilot on a real study."
      footer={
        <>
          <Button variant="ghost" size="md" onClick={onClose}>
            Cancel
          </Button>
          <Button
            type="submit"
            form={formId}
            variant="primary"
            size="md"
            loading={submitting}
            disabled={!canSubmit}
          >
            {submitting ? 'Sending…' : interest === INTEREST_OPTIONS[0] ? 'Request a demo' : 'Send request'}
          </Button>
        </>
      }
    >
      <form id={formId} onSubmit={handleSubmit} noValidate className="flex flex-col gap-4">
        <fieldset className="m-0 min-w-0 border-0 p-0">
          <legend className="mb-1.5 p-0 text-small font-medium text-fg">I&rsquo;d like to</legend>
          <div className="flex flex-wrap gap-2">
            {INTEREST_OPTIONS.map(opt => (
              <Chip key={opt} selected={interest === opt} onClick={() => setInterest(opt)}>
                {opt}
              </Chip>
            ))}
          </div>
        </fieldset>

        <Field label="Company name" required>
          <Input
            type="text"
            value={companyName}
            onChange={e => setCompanyName(e.target.value)}
            placeholder="Acme Pharma"
            autoComplete="organization"
          />
        </Field>

        <Field label="Your name" required>
          <Input
            type="text"
            value={contactName}
            onChange={e => setContactName(e.target.value)}
            placeholder="Jane Doe"
            autoComplete="name"
          />
        </Field>

        <Field label="Work email" required>
          <Input
            type="email"
            value={contactEmail}
            onChange={e => setContactEmail(e.target.value)}
            placeholder="jane@acmepharma.com"
            autoComplete="email"
          />
        </Field>

        <Field label="Team size (optional)">
          <Input
            type="text"
            value={teamSize}
            onChange={e => setTeamSize(e.target.value)}
            placeholder="e.g. 10-25"
          />
        </Field>

        <Field label="What are you looking to do? (optional)">
          <Textarea
            value={message}
            onChange={e => setMessage(e.target.value)}
            placeholder="e.g. a biosimilar PK study for a mAb, and a budget to check against CRO quotes"
            rows={3}
            className="resize-none"
          />
        </Field>

        {error ? <Banner tone="danger">{error}</Banner> : null}
      </form>
    </Modal>
  )
}
