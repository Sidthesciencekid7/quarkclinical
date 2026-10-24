import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const h = vi.hoisted(() => ({ submitLead: vi.fn() }))
vi.mock('../../api/client', () => ({ api: { submitLead: h.submitLead } }))

import ContactSalesModal from '../../components/ContactSalesModal'

const field = (label: RegExp) => screen.getByLabelText(label) as HTMLInputElement

function fillRequired() {
  fireEvent.change(field(/company name/i), { target: { value: ' Acme Pharma ' } })
  fireEvent.change(field(/your name/i), { target: { value: 'Jane Doe' } })
  fireEvent.change(field(/work email/i), { target: { value: 'jane@acmepharma.com' } })
}

describe('ContactSalesModal', () => {
  beforeEach(() => {
    h.submitLead.mockReset()
    h.submitLead.mockResolvedValue({ id: 'lead-1' })
  })
  afterEach(() => {
    document.body.style.overflow = ''
  })

  it('is an accessible dialog with the three interest chips (the initial one pressed)', () => {
    render(<ContactSalesModal initialInterest="Pricing and plans" onClose={() => {}} />)

    expect(screen.getByRole('dialog', { name: 'Book a demo' })).toBeInTheDocument()
    const chips = ['Book a demo', 'Request a pilot on a real study', 'Pricing and plans']
    for (const c of chips) expect(screen.getByRole('button', { name: c })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Pricing and plans' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('button', { name: 'Book a demo' })).toHaveAttribute('aria-pressed', 'false')

    fireEvent.click(screen.getByRole('button', { name: 'Request a pilot on a real study' }))
    expect(screen.getByRole('button', { name: 'Request a pilot on a real study' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
    expect(screen.getByRole('button', { name: 'Pricing and plans' })).toHaveAttribute('aria-pressed', 'false')
  })

  it('keeps the submit button disabled until company, name and email are filled', () => {
    render(<ContactSalesModal onClose={() => {}} />)
    const submit = screen.getByRole('button', { name: 'Request a demo' })
    expect(submit).toBeDisabled()

    fireEvent.change(field(/company name/i), { target: { value: 'Acme' } })
    fireEvent.change(field(/your name/i), { target: { value: 'Jane' } })
    expect(submit).toBeDisabled()
    fireEvent.change(field(/work email/i), { target: { value: 'jane@acme.com' } })
    expect(submit).toBeEnabled()
  })

  it('posts the lead with the interest prefixed onto the message, then thanks the visitor', async () => {
    render(<ContactSalesModal initialInterest="Request a pilot on a real study" onClose={() => {}} />)
    fillRequired()
    fireEvent.change(field(/team size/i), { target: { value: '10-25' } })
    fireEvent.change(screen.getByLabelText(/what are you looking to do/i), {
      target: { value: 'A biosimilar PK study for a mAb' },
    })

    // Non-demo interests relabel the submit button.
    fireEvent.click(screen.getByRole('button', { name: 'Send request' }))

    await waitFor(() => expect(h.submitLead).toHaveBeenCalledTimes(1))
    expect(h.submitLead).toHaveBeenCalledWith({
      company_name: 'Acme Pharma',
      contact_name: 'Jane Doe',
      contact_email: 'jane@acmepharma.com',
      team_size_estimate: '10-25',
      message: '[Request a pilot on a real study] A biosimilar PK study for a mAb',
    })

    expect(await screen.findByRole('dialog', { name: /thanks/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Close' })).toBeInTheDocument()
  })

  it('sends a bare interest tag when no message is given and omits an empty team size', async () => {
    render(<ContactSalesModal onClose={() => {}} />)
    fillRequired()
    fireEvent.click(screen.getByRole('button', { name: 'Request a demo' }))

    await waitFor(() => expect(h.submitLead).toHaveBeenCalledTimes(1))
    expect(h.submitLead.mock.calls[0][0]).toMatchObject({
      message: '[Book a demo]',
      team_size_estimate: undefined,
    })
  })

  it('shows the error and stays on the form when the request fails', async () => {
    h.submitLead.mockRejectedValueOnce(new Error('Server said no'))
    render(<ContactSalesModal onClose={() => {}} />)
    fillRequired()
    fireEvent.click(screen.getByRole('button', { name: 'Request a demo' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('Server said no')
    expect(screen.getByRole('dialog', { name: 'Book a demo' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Request a demo' })).toBeEnabled()
  })

  it('closes on Escape and from the close button', () => {
    const onClose = vi.fn()
    render(<ContactSalesModal onClose={onClose} />)

    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)

    fireEvent.click(screen.getByRole('button', { name: 'Close dialog' }))
    expect(onClose).toHaveBeenCalledTimes(2)
  })
})
