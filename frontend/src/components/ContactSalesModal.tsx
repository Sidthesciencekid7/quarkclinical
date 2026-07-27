import { useState } from 'react'
import { api } from '../api/client'

export default function ContactSalesModal({ onClose }: { onClose: () => void }) {
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
        message: message.trim() || undefined,
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
              We&rsquo;ve received your details and typically respond within one business day to set up
              a call and scope your enterprise license.
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
            <h2 className="text-lg font-bold text-slate-900 mb-1">Talk to sales</h2>
            <p className="text-sm text-slate-500 mb-6 leading-relaxed">
              Tell us about your team and we&rsquo;ll follow up to scope an annual enterprise license.
            </p>

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
                placeholder="Tell us a bit about your protocol workflow today"
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
              {submitting ? 'Sending…' : 'Request a call'}
            </button>
          </>
        )}
      </div>
    </div>
  )
}
