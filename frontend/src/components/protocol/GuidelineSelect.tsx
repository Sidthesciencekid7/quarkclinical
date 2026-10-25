import { useId } from 'react'
import { Select, cx } from '../../ui'
import type { AppGuideline } from '../../types'

export interface GuidelineOption {
  id: string
  /** What the dropdown shows, e.g. "FDA Bioequivalence (ANDA, 2014) · US". */
  label: string
  citation?: string
  /** Submission pathway text, shown under the citation. */
  pathway?: string
}

export interface GuidelineSelectProps {
  options: GuidelineOption[]
  value: string
  onChange: (id: string) => void
  disabled?: boolean
  label?: string
  className?: string
}

/**
 * Regulatory-guideline dropdown (moved out of the retired GenerateButton wizard
 * step). The guideline drives the terminology, acceptance framing and citations
 * the protocol is written against; the citation and submission pathway of the
 * selected option are shown underneath so the choice is never blind.
 */
export function GuidelineSelect({ options, value, onChange, disabled, label = 'Regulatory guideline', className }: GuidelineSelectProps) {
  const id = useId()
  const selected = options.find(o => o.id === value)
  return (
    <div className={cx('flex flex-col gap-1.5', className)}>
      <label htmlFor={id} className="text-small font-medium text-fg">
        {label}
      </label>
      <Select id={id} value={value} disabled={disabled} onChange={e => onChange(e.target.value)}>
        {!selected && value ? <option value={value}>{value}</option> : null}
        {options.map(o => (
          <option key={o.id} value={o.id}>
            {o.label}
          </option>
        ))}
      </Select>
      {selected?.citation ? <p className="text-small text-fg-subtle">{selected.citation}</p> : null}
      {selected?.pathway ? (
        <p className="text-small text-fg-muted">
          <span className="font-medium">Submission pathway:</span> {selected.pathway}
        </p>
      ) : null}
    </div>
  )
}

/**
 * A biosimilar is a different submission from a generic: the FDA profile's
 * headline citation, pathway and label are ANDA (505(j)) wording that does not
 * apply to a 351(k) programme, so show the biosimilar framing when that is the
 * study. Maps the `GET /api/guidelines` shape to select options.
 */
export function guidelineOptionsFrom(guidelines: AppGuideline[], isBiosimilar: boolean): GuidelineOption[] {
  return guidelines.map(g => ({
    id: g.id,
    label: `${(isBiosimilar && g.biosimilar_display_name) || g.display_name} · ${g.region}`,
    citation: (isBiosimilar && g.biosimilar_citation) || g.citation,
    pathway: (isBiosimilar ? g.biosimilar_pathway : g.submission_pathway) || '',
  }))
}

export default GuidelineSelect
