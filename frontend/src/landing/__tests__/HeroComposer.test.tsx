import { createRef } from 'react'
import { fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { clearPendingPrompt, loadPendingPrompt, PENDING_PROMPT_KEY } from '../../lib/pendingPrompt'

const h = vi.hoisted(() => ({
  signup: vi.fn(),
  login: vi.fn(),
  signedIn: false,
}))

vi.mock('../../lib/auth', () => ({
  useLaunch: () => ({ signup: h.signup, login: h.login }),
  authEnabled: false,
}))
vi.mock('../useIsSignedIn', () => ({ useIsSignedIn: () => h.signedIn }))

import { HeroComposer } from '../HeroComposer'

const STUDY = 'Biosimilar PK study of adalimumab in healthy volunteers'

function Where() {
  const loc = useLocation()
  return <p data-testid="where">{`${loc.pathname}${loc.search}`}</p>
}

function renderComposer(textareaRef = createRef<HTMLTextAreaElement>()) {
  render(
    <MemoryRouter initialEntries={['/']}>
      <Routes>
        <Route path="/" element={<HeroComposer textareaRef={textareaRef} />} />
        <Route path="*" element={<Where />} />
      </Routes>
    </MemoryRouter>,
  )
  return textareaRef
}

const box = () => screen.getByRole('textbox', { name: 'Describe your study' }) as HTMLTextAreaElement
const sendButton = () => screen.getByRole('button', { name: 'Start with this study' })

describe('HeroComposer', () => {
  let fetchSpy: ReturnType<typeof vi.fn>

  beforeEach(() => {
    h.signup.mockClear()
    h.login.mockClear()
    h.signedIn = false
    clearPendingPrompt()
    fetchSpy = vi.fn()
    vi.stubGlobal('fetch', fetchSpy)
  })
  afterEach(() => {
    vi.unstubAllGlobals()
    clearPendingPrompt()
  })

  it('fills the box (and the mode) when a suggestion chip is clicked, without sending', () => {
    renderComposer()
    expect(sendButton()).toBeDisabled()

    fireEvent.click(screen.getByRole('button', { name: 'Price a patient study for nivolumab' }))
    expect(box().value).toBe('Price a patient study for nivolumab')
    expect(screen.getByRole('button', { name: 'Budget' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('button', { name: 'Protocol' })).toHaveAttribute('aria-pressed', 'false')

    fireEvent.click(screen.getByRole('button', { name: STUDY }))
    expect(box().value).toBe(STUDY)
    expect(screen.getByRole('button', { name: 'Protocol' })).toHaveAttribute('aria-pressed', 'true')

    // Filling is not sending.
    expect(h.signup).not.toHaveBeenCalled()
    expect(loadPendingPrompt()).toBeNull()
  })

  it('moves focus to the textarea after a chip is clicked', () => {
    const ref = renderComposer()
    fireEvent.click(screen.getByRole('button', { name: STUDY }))
    expect(document.activeElement).toBe(ref.current)
  })

  it('stores the pending prompt and calls signup with a target that never contains the text', () => {
    renderComposer()
    fireEvent.change(box(), { target: { value: STUDY } })
    fireEvent.click(sendButton())

    const stored = loadPendingPrompt()
    expect(stored?.text).toBe(STUDY)
    expect(stored?.kind).toBe('protocol')

    expect(h.signup).toHaveBeenCalledTimes(1)
    const target = h.signup.mock.calls[0][0] as string
    expect(target).toBe('/app/chat/new?from=landing&kind=protocol')
    expect(target).not.toContain('adalimumab')
    expect(decodeURIComponent(target)).not.toContain(STUDY)
    expect(h.login).not.toHaveBeenCalled()
  })

  it('records the Budget mode as the prompt kind', () => {
    renderComposer()
    fireEvent.click(screen.getByRole('button', { name: 'Budget' }))
    fireEvent.change(box(), { target: { value: 'Price a patient study for nivolumab' } })
    fireEvent.click(sendButton())

    expect(loadPendingPrompt()?.kind).toBe('budget')
    expect(h.signup).toHaveBeenCalledWith('/app/chat/new?from=landing&kind=budget')
  })

  it('sends on Enter, but not on Shift+Enter', () => {
    renderComposer()
    fireEvent.change(box(), { target: { value: STUDY } })

    fireEvent.keyDown(box(), { key: 'Enter', shiftKey: true })
    expect(h.signup).not.toHaveBeenCalled()

    fireEvent.keyDown(box(), { key: 'Enter' })
    expect(h.signup).toHaveBeenCalledTimes(1)
  })

  it('does nothing for an empty or whitespace-only box', () => {
    renderComposer()
    fireEvent.change(box(), { target: { value: '   ' } })
    expect(sendButton()).toBeDisabled()
    fireEvent.keyDown(box(), { key: 'Enter' })
    expect(h.signup).not.toHaveBeenCalled()
    expect(localStorage.getItem(PENDING_PROMPT_KEY)).toBeNull()
  })

  it('navigates straight into the app when the visitor already has a session', () => {
    h.signedIn = true
    renderComposer()
    fireEvent.change(box(), { target: { value: STUDY } })
    fireEvent.click(sendButton())

    expect(h.signup).not.toHaveBeenCalled()
    expect(screen.getByTestId('where')).toHaveTextContent('/app/chat/new?from=landing&kind=protocol')
    expect(screen.getByTestId('where').textContent).not.toContain('adalimumab')
    expect(loadPendingPrompt()?.text).toBe(STUDY)
  })

  it('makes no network request from the composer', () => {
    renderComposer()
    fireEvent.click(screen.getByRole('button', { name: STUDY }))
    fireEvent.click(sendButton())
    expect(fetchSpy).not.toHaveBeenCalled()
  })

  it('says so, and does not leave, when the browser blocks storage; "Continue anyway" proceeds', () => {
    const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    renderComposer()
    fireEvent.change(box(), { target: { value: STUDY } })
    fireEvent.click(sendButton())

    expect(setItem).toHaveBeenCalled()
    expect(h.signup).not.toHaveBeenCalled()
    expect(screen.getByRole('alert')).toHaveTextContent(/blocking local storage/i)

    fireEvent.click(screen.getByRole('button', { name: 'Continue anyway' }))
    expect(h.signup).toHaveBeenCalledWith('/app/chat/new?from=landing&kind=protocol')
  })
})
