import { useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import { Badge, Button, Input, cx } from '../ui'
import type { BadgeVariant } from '../ui'
import { USER_INDICATION_MAX_CHARS } from '../types'
import type { Drug, IndicationCandidate, PatientPopulationPreview, StudyType } from '../types'

interface Props {
  drug: Drug
  studyType: StudyType
  value: string
  onChange: (text: string) => void
  className?: string
}

const DEBOUNCE_MS = 700

// Control characters, zero-width / bidi marks and line/paragraph separators are
// stripped client-side; the server sanitises again and is authoritative.
const UNSAFE_CHARS = /[\u0000-\u001F\u007F-\u009F\u200B-\u200F\u2028\u2029]/g

/** Strip unsafe characters and cap at the indication limit (used by every indication input). */
export function cleanIndication(text: string): string {
  return text.replace(UNSAFE_CHARS, ' ').replace(/ {2,}/g, ' ').slice(0, USER_INDICATION_MAX_CHARS)
}

export const SETTING_LABELS: Record<string, string> = {
  adjuvant: 'Adjuvant (post-surgery)',
  low_burden: 'Low tumour burden',
  high_burden: 'Metastatic / high tumour burden',
  not_applicable: 'Non-oncology',
  unknown: 'Setting not established',
}

export const MONO_LABELS: Record<string, string> = {
  yes: 'Monotherapy',
  possible: 'Monotherapy possible',
  no: 'With background therapy',
}

/** The candidate fields the badges read (wizard `IndicationCandidate` and the chat card's candidate both fit). */
export interface CandidateBadgeSource {
  on_label: boolean
  tumor_setting?: string | null
  monotherapy?: string | null
}

export function CandidateBadges({ c, className }: { c: CandidateBadgeSource; className?: string }) {
  const setting = c.tumor_setting
  const settingTone: BadgeVariant =
    setting === 'adjuvant' || setting === 'low_burden' || setting === 'not_applicable' ? 'success' : setting === 'high_burden' ? 'warning' : 'neutral'
  const mono = c.monotherapy ?? ''
  const monoTone: BadgeVariant = mono === 'yes' ? 'success' : mono === 'possible' ? 'info' : 'warning'
  return (
    <div className={cx('mt-1.5 flex flex-wrap items-center gap-1.5', className)}>
      {c.on_label ? <Badge variant="success">On label</Badge> : <Badge variant="warning">Not confirmed on label</Badge>}
      {setting ? <Badge variant={settingTone}>{SETTING_LABELS[setting] ?? setting}</Badge> : null}
      {MONO_LABELS[mono] ? <Badge variant={monoTone}>{MONO_LABELS[mono]}</Badge> : null}
    </div>
  )
}

/**
 * Live indication ranking for a patient biosimilar PK study: type an
 * indication (or keep the engine's pick) and see it checked against the label.
 * Self-contained: it fetches the credit-free `indication-preview` itself. The
 * chat indication card shows the same information from the server's card data
 * and re-uses `CandidateBadges` / `cleanIndication` from here.
 */
export default function PatientIndicationPanel({ drug, studyType, value, onChange, className }: Props) {
  const [draft, setDraft] = useState(value)
  const [preview, setPreview] = useState<PatientPopulationPreview | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [showAll, setShowAll] = useState(false)

  const onChangeRef = useRef(onChange)
  onChangeRef.current = onChange
  const seq = useRef(0)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const firstDraft = useRef(true)

  const load = (text: string) => {
    const mine = ++seq.current
    setLoading(true)
    setError(null)
    api
      .indicationPreview(drug.id, studyType, text || undefined)
      .then(res => {
        if (mine === seq.current) setPreview(res)
      })
      .catch(e => {
        if (mine === seq.current) setError(e instanceof Error ? e.message : 'request failed')
      })
      .finally(() => {
        if (mine === seq.current) setLoading(false)
      })
  }

  useEffect(() => {
    load(value)
    return () => {
      seq.current++
      if (timer.current) clearTimeout(timer.current)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [drug.id, studyType])

  const draftRef = useRef(draft)
  draftRef.current = draft
  const pending = useRef(false)

  useEffect(() => {
    if (firstDraft.current) {
      firstDraft.current = false
      return
    }
    if (timer.current) clearTimeout(timer.current)
    pending.current = true
    timer.current = setTimeout(() => {
      pending.current = false
      const text = cleanIndication(draft).trim()
      onChangeRef.current(text)
      load(text)
    }, DEBOUNCE_MS)
    return () => {
      if (timer.current) clearTimeout(timer.current)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draft])

  // Commit immediately (not after the debounce) on blur and on unmount, so
  // typing an indication and moving on straight away never drops it.
  const flush = () => {
    if (!pending.current) return
    pending.current = false
    if (timer.current) clearTimeout(timer.current)
    const text = cleanIndication(draftRef.current).trim()
    onChangeRef.current(text)
    load(text)
  }
  useEffect(
    () => () => {
      if (!pending.current) return
      pending.current = false
      onChangeRef.current(cleanIndication(draftRef.current).trim())
    },
    [],
  )

  const engine = preview?.engine_recommended ?? null
  const ranked = preview?.ranked ?? []
  const unavailable = error !== null || (preview !== null && preview.applicable === false)
  const unavailableReason = error ?? preview?.reason ?? 'not applicable to this product'

  const badgeSource = (c: IndicationCandidate): CandidateBadgeSource => ({ on_label: c.on_label, tumor_setting: c.tumor_setting, monotherapy: c.monotherapy })

  return (
    <div className={cx('space-y-3 rounded-input border border-line bg-surface px-4 py-3.5', className)}>
      <div>
        <span className="text-body font-semibold text-fg">Study indication</span>
        <p className="mt-1 text-small text-fg-muted">
          Ranked by the engine: on-label, then biologic-naive, then lowest-variability setting (adjuvant / low tumour burden / non-oncology),
          then monotherapy, lowest PK variability and recruitability. You may enter your own indication instead; it is checked against the
          label and compared with the engine&apos;s pick.
        </p>
      </div>

      <div>
        <div className="flex items-center justify-between">
          <label htmlFor="user-indication" className="text-eyebrow uppercase text-fg-subtle">
            Your indication (optional)
          </label>
          <span className="text-small tabular-nums text-fg-subtle">
            {draft.length}/{USER_INDICATION_MAX_CHARS}
          </span>
        </div>
        <div className="mt-1.5 flex gap-2">
          <Input
            id="user-indication"
            type="text"
            maxLength={USER_INDICATION_MAX_CHARS}
            value={draft}
            onChange={e => setDraft(cleanIndication(e.target.value))}
            onBlur={flush}
            placeholder="e.g. Resected stage III melanoma (adjuvant, monotherapy)"
          />
          <Button variant="ghost" onClick={() => setDraft('')} disabled={draft === ''}>
            Clear
          </Button>
        </div>
      </div>

      {loading && !preview && !error ? <div className="h-14 animate-pulse rounded-input border border-line bg-surface-muted" /> : null}

      {unavailable ? (
        <div className="rounded-input border border-line bg-surface-muted px-3.5 py-2.5 text-small text-fg-muted">
          Indication ranking unavailable: {unavailableReason}
        </div>
      ) : null}

      {!unavailable && preview && engine ? (
        <div className="rounded-input border border-[color:var(--info-border)] bg-[var(--info-bg)] px-3.5 py-2.5">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-body font-semibold text-fg">{engine.indication}</span>
            <Badge variant="info">Engine recommendation</Badge>
          </div>
          <CandidateBadges c={badgeSource(engine)} />
          {engine.rank_reason ? <p className="mt-1.5 text-small text-fg-muted">{engine.rank_reason}</p> : null}
        </div>
      ) : null}

      {!unavailable && preview?.user_specified && preview.recommended ? (
        <div className="rounded-input border border-line-strong bg-surface px-3.5 py-2.5">
          <p className="text-eyebrow uppercase text-fg-subtle">Your indication (locked into the protocol)</p>
          <p className="mt-1 text-body font-semibold text-fg">{preview.recommended.indication}</p>
          <CandidateBadges c={badgeSource(preview.recommended)} />
          {(preview.user_indication_warnings ?? []).map((w, i) => (
            <div key={i} className="mt-2 rounded-input border border-[color:var(--warn-border)] bg-[var(--warn-bg)] px-3 py-2 text-small text-[color:var(--warn-fg)]">
              {w}
            </div>
          ))}
          {engine ? <p className="mt-2 text-small text-fg-muted">Engine recommendation for comparison: {engine.indication}</p> : null}
        </div>
      ) : null}

      {!unavailable && ranked.length > 0 ? (
        <div>
          <Button variant="link" size="sm" onClick={() => setShowAll(v => !v)} aria-expanded={showAll}>
            {showAll ? 'Hide candidates' : `Show all ${ranked.length} candidates`}
          </Button>
          {showAll ? (
            <ul className="m-0 mt-2 list-none space-y-2 p-0">
              {ranked.map((c, i) => (
                <li key={`${c.indication}-${i}`} className="flex items-start gap-3 rounded-input border border-line px-3.5 py-2.5">
                  <div className="min-w-0 flex-1">
                    <p className="text-body font-semibold text-fg">{c.indication}</p>
                    <CandidateBadges c={badgeSource(c)} />
                    {c.rank_reason ? <p className="mt-1.5 text-small text-fg-muted">{c.rank_reason}</p> : null}
                  </div>
                  <Button variant="secondary" size="sm" className="flex-none" onClick={() => setDraft(i === 0 ? '' : c.indication)}>
                    Use this
                  </Button>
                </li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}
    </div>
  )
}
