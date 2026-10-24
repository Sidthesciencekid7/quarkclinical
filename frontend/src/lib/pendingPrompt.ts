/**
 * Landing-page handoff: the visitor types a study description before they have
 * an account, so it is parked in localStorage across the sign-up round trip and
 * picked up by /app/chat/new?from=landing.
 *
 * Why localStorage (not sessionStorage): Clerk's email verification can open the
 * app in a NEW tab, which would not see this tab's sessionStorage.
 * Why never the URL: the text is user-typed and may be sensitive; it must not
 * reach history, referrers, analytics or server logs.
 *
 * Every storage access is guarded -- private windows, blocked site data and
 * quota errors all make localStorage throw, and none of that may break the page.
 * Nothing here is ever sent anywhere; the consumer decides what to do with it.
 */

export type PendingPromptKind = 'protocol' | 'budget'

export interface PendingPrompt {
  text: string
  kind: PendingPromptKind
  /** Epoch milliseconds. */
  createdAt: number
}

/** Versioned so a future shape change can simply ignore old values. */
export const PENDING_PROMPT_KEY = 'qc_pending_prompt_v1'
export const PENDING_PROMPT_MAX_CHARS = 2000
export const PENDING_PROMPT_TTL_MS = 7 * 24 * 60 * 60 * 1000

/** Stored shape: `{v:1, text, kind, createdAt}`. */
interface StoredPendingPrompt {
  v: 1
  text: string
  kind: PendingPromptKind
  createdAt: number
}

/** Tolerated clock skew before a "from the future" timestamp is treated as corrupt. */
const FUTURE_SKEW_MS = 24 * 60 * 60 * 1000

// C0 controls except \n (\t is stripped too), DEL, and C1 controls. \r is
// dropped, so CRLF input collapses to \n.
const CONTROL_CHARS = /[\u0000-\u0009\u000B-\u001F\u007F-\u009F]/g

function getStorage(): Storage | null {
  try {
    return typeof window !== 'undefined' ? window.localStorage : null
  } catch {
    return null
  }
}

/** Strip control characters (keeping \n), trim, and cap at 2000 characters
 *  (counted in code points, so an emoji is never cut in half). */
export function sanitizePendingPromptText(raw: unknown): string {
  if (typeof raw !== 'string') return ''
  const cleaned = raw.replace(CONTROL_CHARS, '').trim()
  const points = Array.from(cleaned)
  const capped = points.length > PENDING_PROMPT_MAX_CHARS
    ? points.slice(0, PENDING_PROMPT_MAX_CHARS).join('')
    : cleaned
  return capped.trim()
}

function isKind(value: unknown): value is PendingPromptKind {
  return value === 'protocol' || value === 'budget'
}

/** Save (replacing any earlier value). Returns true if a prompt was stored;
 *  false when the text is empty after sanitising or storage is unavailable. */
export function savePendingPrompt(input: { text: string; kind: PendingPromptKind }): boolean {
  const storage = getStorage()
  if (!storage) return false
  const text = sanitizePendingPromptText(input?.text)
  if (!text) {
    clearPendingPrompt()
    return false
  }
  const value: StoredPendingPrompt = {
    v: 1,
    text,
    kind: isKind(input.kind) ? input.kind : 'protocol',
    createdAt: Date.now(),
  }
  try {
    storage.setItem(PENDING_PROMPT_KEY, JSON.stringify(value))
    return true
  } catch {
    return false
  }
}

/** The saved prompt, or null when there is none, it is older than 7 days, or
 *  the stored value is unreadable or malformed (expired and corrupt values are
 *  also removed). Does NOT clear a valid prompt: the consumer calls
 *  clearPendingPrompt() once it has safely handed the text on. */
export function loadPendingPrompt(): PendingPrompt | null {
  const storage = getStorage()
  if (!storage) return null
  let rawValue: string | null
  try {
    rawValue = storage.getItem(PENDING_PROMPT_KEY)
  } catch {
    return null
  }
  if (rawValue === null) return null

  let parsed: unknown
  try {
    parsed = JSON.parse(rawValue)
  } catch {
    clearPendingPrompt()
    return null
  }
  const v = parsed as Partial<StoredPendingPrompt> | null
  if (
    !v ||
    typeof v !== 'object' ||
    v.v !== 1 ||
    !isKind(v.kind) ||
    typeof v.createdAt !== 'number' ||
    !Number.isFinite(v.createdAt)
  ) {
    clearPendingPrompt()
    return null
  }

  const now = Date.now()
  if (now - v.createdAt > PENDING_PROMPT_TTL_MS || v.createdAt - now > FUTURE_SKEW_MS) {
    clearPendingPrompt()
    return null
  }

  // localStorage is user-editable: sanitise again on the way out.
  const text = sanitizePendingPromptText(v.text)
  if (!text) {
    clearPendingPrompt()
    return null
  }
  return { text, kind: v.kind, createdAt: v.createdAt }
}

export function clearPendingPrompt(): void {
  const storage = getStorage()
  if (!storage) return
  try {
    storage.removeItem(PENDING_PROMPT_KEY)
  } catch {
    /* storage unavailable -- nothing to clear */
  }
}
