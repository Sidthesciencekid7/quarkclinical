import { useState } from 'react'
import { api } from '../api/client'

const INTEREST_OPTIONS = ['Book a demo', 'Request a pilot on a real study', 'Pricing and plans']

export default function ContactSalesModal({
  onClose,
  initialInterest = INTEREST_OPTIONS[0],
}: {
  onClose: () => void
  initialInterest?: string
}) {
  const [interest, setInterest] = useState(initialInterest)
  const [companyName, setCompanyName] = useState('')
  const [contactName, setContactName] = useState('')
  const [contactEmail, setContactEmail] = useState('')
  const [teamSize, setTeamSize] = useState('')
  const [message, setMessage] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [sent, setSent] = useState(false)

  const canSubmit = companyName.trim() && contactName.trim() && contactEmail.trim()

  const handleSubmit = async () => {
    if (!canSubmit) return
    setSubmitting(true)
    setError(null)
    try {
      await api.submitLead({
        company_name: companyName.trim(),
        contact_name: contactName.trim(),
        contact_email: contactEmail.trim(),
        team_size_estimate: teamSize.trim() || undefined,
        // The leads table has no interest column; prefix it onto the message
        // so the operator sees what the prospect asked for.
        message: `[${interest}]${message.trim() ? ' ' + message.trim() : ''}`,
      })
      setSent(true)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not send your request — please try again.')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div
      className="fixed inset-0 z-[100] flex items-center justify-center px-4 bg-slate-900/60 backdrop-blur-sm"
      onClick={onClose}
    >
      <div
        className="bg-white rounded-2xl border border-slate-200/80 shadow-2xl max-w-md w-full p-8 relative"
        onClick={e => e.stopPropagation()}
      >
        <button
          type="button"
          onClick={onClose}
          aria-label="Close"
          className="absolute top-4 right-4 text-slate-400 hover:text-slate-600 cursor-pointer text-xl leading-none"
        >
          &times;
        </button>

        {sent ? (
          <div className="py-4">
            <h2 className="text-lg font-bold text-slate-900 mb-2">Thanks — we&rsquo;ll be in touch</h2>
            <p className="text-sm text-slate-500 leading-relaxed">
              We&rsquo;ve received your request and will reply by email to arrange a time.
            </p>
            <button
              type="button"
              onClick={onClose}
              className="mt-6 w-full py-3 rounded-xl font-bold text-sm text-white bg-cyan-600 hover:bg-cyan-700 transition-colors cursor-pointer"
            >
              Close
            </button>
          </div>
        ) : (
          <>
            <h2 className="text-lg font-bold text-slate-900 mb-1">Book a demo</h2>
            <p className="text-sm text-slate-500 mb-5 leading-relaxed">
              See QuarkClinical on a study like yours. We&rsquo;ll walk through a protocol and budget for a
              drug you choose, or scope a pilot on a real study.
            </p>

            <fieldset className="mb-5">
              <legend className="block text-xs font-semibold text-slate-500 mb-1.5">I&rsquo;d like to</legend>
              <div className="flex flex-wrap gap-2">
                {INTEREST_OPTIONS.map(opt => (
                  <button
                    key={opt}
                    type="button"
                    onClick={() => setInterest(opt)}
                    aria-pressed={interest === opt}
                    className={`px-3 py-1.5 rounded-lg text-xs font-semibold border transition-colors cursor-pointer ${
                      interest === opt
                        ? 'border-cyan-500 bg-cyan-50 text-cyan-800'
                        : 'border-slate-200 text-slate-600 hover:border-slate-300'
                    }`}
                  >
                    {opt}
                  </button>
                ))}
              </div>
            </fieldset>

            <div className="mb-4">
              <label className="block text-xs font-semibold text-slate-500 mb-1.5">Company name</label>
              <input
                type="text"
                value={companyName}
                onChange={e => setCompanyName(e.target.value)}
                placeholder="Acme Pharma"
                className="input-base"
              />
            </div>

            <div className="mb-4">
              <label className="block text-xs font-semibold text-slate-500 mb-1.5">Your name</label>
              <input
                type="text"
                value={contactName}
                onChange={e => setContactName(e.target.value)}
                placeholder="Jane Doe"
                className="input-base"
              />
            </div>

            <div className="mb-4">
              <label className="block text-xs font-semibold text-slate-500 mb-1.5">Work email</label>
              <input
                type="email"
                value={contactEmail}
                onChange={e => setContactEmail(e.target.value)}
                placeholder="jane@acmepharma.com"
                className="input-base"
              />
            </div>

            <div className="mb-4">
              <label className="block text-xs font-semibold text-slate-500 mb-1.5">
                Team size <span className="font-normal text-slate-400">(optional)</span>
              </label>
              <input
                type="text"
                value={teamSize}
                onChange={e => setTeamSize(e.target.value)}
                placeholder="e.g. 10-25"
                className="input-base"
              />
            </div>

            <div className="mb-2">
              <label className="block text-xs font-semibold text-slate-500 mb-1.5">
                What are you looking to do? <span className="font-normal text-slate-400">(optional)</span>
              </label>
              <textarea
                value={message}
                onChange={e => setMessage(e.target.value)}
                placeholder="e.g. a biosimilar PK study for a mAb, and a budget to check against CRO quotes"
                rows={3}
                className="input-base resize-none"
              />
            </div>

            {error && <p className="text-xs text-red-500 mt-2">{error}</p>}

            <button
              type="button"
              onClick={handleSubmit}
              disabled={submitting || !canSubmit}
              className="mt-5 w-full py-3 rounded-xl font-bold text-sm text-white bg-cyan-600 hover:bg-cyan-700 disabled:opacity-50 transition-colors cursor-pointer"
            >
              {submitting ? 'Sending…' : interest === INTEREST_OPTIONS[0] ? 'Request a demo' : 'Send request'}
            </button>
          </>
        )}
      </div>
    </div>
  )
}
