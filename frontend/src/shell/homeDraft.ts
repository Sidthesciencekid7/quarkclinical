/**
 * Home-composer handoff (D: "Sending creates a session with the text as the
 * first message"). Chat sessions do not exist yet, so the Home composer parks
 * the typed text in **sessionStorage** under `qc_home_draft` and navigates to
 * `/app/chat/new?kind=<protocol|budget>&from=home`.
 *
 * Why sessionStorage and not router state: it survives a reload of the
 * placeholder/chat page, which router state does not. Why not the URL: the
 * text is user-typed and may be sensitive (same rule as the landing handoff in
 * lib/pendingPrompt.ts). Why not localStorage: unlike the landing prompt there
 * is no sign-up round trip to survive, so it should not outlive the tab.
 *
 * The consumer (NewChatPage in the chat wave) calls `loadHomeDraft()`, creates
 * the session with `initial_message`, then `clearHomeDraft()`.
 *
 * Stored shape: JSON `{ v: 1, text, kind, createdAt }`.
 */
import { sanitizePendingPromptText } from '../lib/pendingPrompt'
import type { StudyKind } from './config'

export const HOME_DRAFT_KEY = 'qc_home_draft'

export interface HomeDraft {
  text: string
  kind: StudyKind
  createdAt: number
}

function storage(): Storage | null {
  try {
    return typeof window !== 'undefined' ? window.sessionStorage : null
  } catch {
    return null
  }
}

export function saveHomeDraft(input: { text: string; kind: StudyKind }): boolean {
  const s = storage()
  if (!s) return false
  const text = sanitizePendingPromptText(input.text)
  if (!text) return false
  try {
    s.setItem(HOME_DRAFT_KEY, JSON.stringify({ v: 1, text, kind: input.kind, createdAt: Date.now() }))
    return true
  } catch {
    return false
  }
}

export function loadHomeDraft(): HomeDraft | null {
  const s = storage()
  if (!s) return null
  try {
    const raw = s.getItem(HOME_DRAFT_KEY)
    if (!raw) return null
    const v = JSON.parse(raw) as Partial<{ v: number; text: string; kind: string; createdAt: number }> | null
    if (!v || v.v !== 1 || (v.kind !== 'protocol' && v.kind !== 'budget') || typeof v.createdAt !== 'number') return null
    const text = sanitizePendingPromptText(v.text)
    if (!text) return null
    return { text, kind: v.kind, createdAt: v.createdAt }
  } catch {
    return null
  }
}

export function clearHomeDraft(): void {
  try {
    storage()?.removeItem(HOME_DRAFT_KEY)
  } catch {
    /* nothing to clear */
  }
}
