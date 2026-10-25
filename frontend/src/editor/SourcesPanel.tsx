import { useId, useMemo } from 'react'
import { Badge, Button, ChevronDown, cx } from '../ui'

interface Props {
  sections: Record<string, string>
  open: boolean
  onToggle: () => void
}

function extractPMIDs(sections: Record<string, string>): string[] {
  const text = Object.values(sections).join(' ')
  const matches = Array.from(text.matchAll(/PMID[:\s]+(\d{7,8})/gi))
  return [...new Set(matches.map(m => m[1]))]
}

function extractFDARefs(sections: Record<string, string>): string[] {
  const text = Object.values(sections).join(' ')
  const matches = Array.from(
    text.matchAll(/FDA (?:Draft )?Guidance[^.;,\n]{0,100}/gi),
  )
  return [...new Set(matches.map(m => m[0].trim()))]
}

function extractCFRRefs(sections: Record<string, string>): string[] {
  const text = Object.values(sections).join(' ')
  const matches = Array.from(text.matchAll(/\d+ CFR [\d.]+(?:\(\w+\))*/g))
  return [...new Set(matches.map(m => m[0]))]
}

export default function SourcesPanel({ sections, open, onToggle }: Props) {
  const pmids = useMemo(() => extractPMIDs(sections), [sections])
  const fdaRefs = useMemo(() => extractFDARefs(sections), [sections])
  const cfrRefs = useMemo(() => extractCFRRefs(sections), [sections])
  const total = pmids.length + fdaRefs.length + cfrRefs.length
  const bodyId = useId()

  return (
    <div className="flex-none border-t border-line bg-surface">
      {/* Toggle bar */}
      <div className="flex items-center gap-2 px-3 py-1.5">
        <Button
          variant="ghost"
          size="sm"
          onClick={onToggle}
          aria-expanded={open}
          aria-controls={bodyId}
          leftIcon={<ChevronDown aria-hidden className={cx('transition-transform duration-fast', open && 'rotate-180')} />}
        >
          Sources &amp; references
        </Button>
        {total > 0 ? (
          <Badge variant="neutral">{total}</Badge>
        ) : (
          <span className="text-small text-fg-subtle">None extracted yet</span>
        )}
      </div>

      {open ? (
        <div id={bodyId} className="h-52 overflow-y-auto border-t border-line">
          <div className="grid grid-cols-1 gap-6 px-5 py-4 sm:grid-cols-3">
            {/* PubMed */}
            <div>
              <h3 className="eyebrow mb-2">PubMed ({pmids.length})</h3>
              {pmids.length === 0 ? (
                <p className="text-small text-fg-subtle">None extracted</p>
              ) : (
                <ul className="space-y-1">
                  {pmids.map(pmid => (
                    <li key={pmid}>
                      <a
                        href={`https://pubmed.ncbi.nlm.nih.gov/${pmid}/`}
                        target="_blank"
                        rel="noreferrer noopener"
                        className="rounded text-small text-accent-700 underline-offset-2 hover:underline"
                      >
                        PMID: {pmid}
                      </a>
                    </li>
                  ))}
                </ul>
              )}
            </div>

            {/* FDA */}
            <div>
              <h3 className="eyebrow mb-2">FDA guidances ({fdaRefs.length})</h3>
              {fdaRefs.length === 0 ? (
                <p className="text-small text-fg-subtle">None extracted</p>
              ) : (
                <ul className="space-y-1.5">
                  {fdaRefs.map((ref, i) => (
                    <li key={i} className="text-small text-fg-muted">
                      {ref}
                    </li>
                  ))}
                </ul>
              )}
            </div>

            {/* CFR */}
            <div>
              <h3 className="eyebrow mb-2">CFR citations ({cfrRefs.length})</h3>
              {cfrRefs.length === 0 ? (
                <p className="text-small text-fg-subtle">None extracted</p>
              ) : (
                <ul className="space-y-1.5">
                  {cfrRefs.map((ref, i) => (
                    <li key={i} className="font-mono text-small text-fg-muted">
                      {ref}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>
        </div>
      ) : null}
    </div>
  )
}
