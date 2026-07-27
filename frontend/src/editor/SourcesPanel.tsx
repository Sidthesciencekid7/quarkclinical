import { useMemo } from 'react'

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

  return (
    <div className="flex-shrink-0 border-t border-slate-200 bg-white">
      {/* Toggle bar */}
      <button
        onClick={onToggle}
        className="w-full px-5 py-2.5 flex items-center gap-2 hover:bg-slate-50 transition-colors"
      >
        <svg
          width="12"
          height="12"
          viewBox="0 0 12 12"
          fill="none"
          className={`flex-shrink-0 text-slate-400 transition-transform ${open ? 'rotate-180' : ''}`}
        >
          <path
            d="M2 4l4 4 4-4"
            stroke="currentColor"
            strokeWidth="1.75"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
        <span className="text-[11px] font-semibold text-slate-600">Sources &amp; References</span>
        {total > 0 && (
          <span className="text-[10px] bg-slate-100 text-slate-500 px-1.5 py-0.5 rounded-full font-medium">
            {total}
          </span>
        )}
        {total === 0 && (
          <span className="text-[10px] text-slate-400">None extracted yet</span>
        )}
      </button>

      {open && (
        <div className="h-52 overflow-y-auto border-t border-slate-100">
          <div className="grid grid-cols-3 gap-6 px-5 py-4">
            {/* PubMed */}
            <div>
              <p className="text-[10px] font-semibold text-slate-400 uppercase tracking-wider mb-2">
                PubMed ({pmids.length})
              </p>
              {pmids.length === 0 ? (
                <p className="text-xs text-slate-400">None extracted</p>
              ) : (
                <ul className="space-y-1">
                  {pmids.map(pmid => (
                    <li key={pmid}>
                      <a
                        href={`https://pubmed.ncbi.nlm.nih.gov/${pmid}/`}
                        target="_blank"
                        rel="noreferrer noopener"
                        className="text-xs text-indigo-600 hover:text-indigo-800 hover:underline font-mono"
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
              <p className="text-[10px] font-semibold text-slate-400 uppercase tracking-wider mb-2">
                FDA Guidances ({fdaRefs.length})
              </p>
              {fdaRefs.length === 0 ? (
                <p className="text-xs text-slate-400">None extracted</p>
              ) : (
                <ul className="space-y-1.5">
                  {fdaRefs.map((ref, i) => (
                    <li key={i} className="text-xs text-slate-600 leading-snug">
                      {ref}
                    </li>
                  ))}
                </ul>
              )}
            </div>

            {/* CFR */}
            <div>
              <p className="text-[10px] font-semibold text-slate-400 uppercase tracking-wider mb-2">
                CFR Citations ({cfrRefs.length})
              </p>
              {cfrRefs.length === 0 ? (
                <p className="text-xs text-slate-400">None extracted</p>
              ) : (
                <ul className="space-y-1.5">
                  {cfrRefs.map((ref, i) => (
                    <li key={i} className="text-xs text-slate-600 font-mono">
                      {ref}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
