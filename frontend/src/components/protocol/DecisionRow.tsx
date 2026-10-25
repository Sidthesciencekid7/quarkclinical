import type { ReactNode } from 'react'
import { Activity, Badge, ClipboardList, FlaskConical, Select, Spinner, Syringe, Users, cx } from '../../ui'
import { POP_PATIENTS_MULTI } from '../../types'

/** The fields of one study-design decision (wizard `DesignDecision` and chat `DesignDecisionData` both fit). */
export interface DecisionRowDecision {
  key: string
  label: string
  value: string
  rationale?: string
  options?: string[]
}

export interface DecisionRowProps {
  decision: DecisionRowDecision
  /** Called with the chosen option. Not called for the current value. */
  onChange?: (value: string) => void
  /** Controls are inert (card superseded / done, or a turn is streaming). */
  disabled?: boolean
  /** This row's change is being saved: shows a spinner next to the label. */
  pending?: boolean
  className?: string
}

const ICONS: Record<string, ReactNode> = {
  population: <Users />,
  dose: <FlaskConical />,
  route: <Syringe />,
  design: <ClipboardList />,
  pd_endpoint: <Activity />,
}

/**
 * One of the five study-design decisions (moved out of the retired
 * ReviewDesign wizard step): label, the engine's rationale, and the choice.
 * A decision with a single option is shown as fixed text. Every value comes
 * from the server; the select only offers that decision's own options.
 */
export function DecisionRow({ decision, onChange, disabled, pending, className }: DecisionRowProps) {
  const options = decision.options ?? []
  const choosable = options.length > 1
  const selectLabel = `${decision.label}`
  return (
    <div className={cx('flex items-start gap-3.5 rounded-input border border-line bg-surface px-4 py-3.5', className)}>
      <span
        aria-hidden
        className="mt-0.5 flex h-9 w-9 flex-none items-center justify-center rounded-xl bg-surface-muted text-fg-muted [&>svg]:h-4 [&>svg]:w-4"
      >
        {ICONS[decision.key] ?? ICONS.design}
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-body font-semibold text-fg">{decision.label}</span>
          <Badge variant="info">Engine</Badge>
          {pending ? <Spinner size={14} label="Saving" className="text-fg-subtle" /> : null}
        </div>
        {decision.rationale ? <p className="mt-1 text-small text-fg-muted">{decision.rationale}</p> : null}
        {choosable ? (
          <div className="mt-2">
            <Select
              aria-label={selectLabel}
              value={decision.value}
              disabled={disabled || pending}
              onChange={e => {
                if (e.target.value !== decision.value) onChange?.(e.target.value)
              }}
            >
              {options.includes(decision.value) ? null : <option value={decision.value}>{decision.value}</option>}
              {options.map(o => (
                <option key={o} value={o}>
                  {o}
                </option>
              ))}
            </Select>
          </div>
        ) : (
          <p className="mt-2 text-body font-semibold text-fg">{decision.value}</p>
        )}
        {decision.key === 'population' && decision.value === POP_PATIENTS_MULTI ? (
          <p className="mt-1.5 text-small text-fg-muted">
            PK is evaluated at steady state under the labelled multiple-dose regimen (first-dose and steady-state AUC(0-tau) co-primary).
          </p>
        ) : null}
      </div>
    </div>
  )
}

export default DecisionRow
