import { useEffect, useRef, useState } from 'react'
import { api, HttpError } from '../api/client'
import type { Drug, GeneratedProtocol } from '../types'

interface Props {
  protocol: GeneratedProtocol
  drug: Drug
}

type FileFormat = 'docx' | 'pdf' | 'csv'

interface DownloadSpec {
  key: string
  label: string
  format: FileFormat
  /** Restricted documents need an explicit confirmation before downloading. */
  restricted?: boolean
  fetch: (protocolId: string) => Promise<Blob>
  /** Filename stem; the format is appended. */
  stem: string
}

/** The protocol document itself. */
const PROTOCOL_DOWNLOADS: DownloadSpec[] = [
  { key: 'protocol-docx', label: 'Protocol (.docx)', format: 'docx', fetch: api.exportDocx, stem: 'protocol' },
  { key: 'protocol-pdf', label: 'Protocol (.pdf)', format: 'pdf', fetch: api.exportPdf, stem: 'protocol' },
]

/** Standalone documents that travel WITH the protocol but are not part of it:
 *  the ICF carries its own version/date for the IRB, the CRF specification and
 *  Data Management Plan are the data-capture appendix, and the randomisation
 *  list is the restricted unblinded allocation that must be filed separately
 *  (ICH E9 allocation concealment) rather than bound into the protocol body. */
const PACKAGE_DOWNLOADS: DownloadSpec[] = [
  { key: 'icf-docx', label: 'Informed Consent Form (.docx)', format: 'docx', fetch: api.exportIcfDocx, stem: 'icf' },
  { key: 'icf-pdf', label: 'Informed Consent Form (.pdf)', format: 'pdf', fetch: api.exportIcfPdf, stem: 'icf' },
  { key: 'crf-docx', label: 'CRF spec + Data Mgmt Plan (.docx)', format: 'docx', fetch: api.exportCrfDmpDocx, stem: 'crf_dmp' },
  { key: 'crf-pdf', label: 'CRF spec + Data Mgmt Plan (.pdf)', format: 'pdf', fetch: api.exportCrfDmpPdf, stem: 'crf_dmp' },
  {
    key: 'randomization-csv',
    label: 'Randomisation list (.csv)',
    format: 'csv',
    restricted: true,
    fetch: api.exportRandomizationCsv,
    stem: 'randomisation_list_RESTRICTED',
  },
]

const ALL_DOWNLOADS = [...PROTOCOL_DOWNLOADS, ...PACKAGE_DOWNLOADS]

function slug(inn: string): string {
  return inn.trim().replace(/\s+/g, '_')
}

export default function ExportButton({ protocol, drug }: Props) {
  const [open, setOpen] = useState(false)
  const [downloading, setDownloading] = useState<string | null>(null)
  const [confirmKey, setConfirmKey] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const wrapperRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (wrapperRef.current && !wrapperRef.current.contains(e.target as Node)) {
        setOpen(false)
        setConfirmKey(null)
      }
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [])

  useEffect(() => {
    if (!error) return
    const t = setTimeout(() => setError(null), 6000)
    return () => clearTimeout(t)
  }, [error])

  const handleDownload = async (spec: DownloadSpec) => {
    setOpen(false)
    setConfirmKey(null)
    setError(null)
    setDownloading(spec.key)
    try {
      const blob = await spec.fetch(protocol.id)
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `${spec.stem}_${slug(drug.inn)}_v${protocol.version}.${spec.format}`
      document.body.appendChild(a)
      a.click()
      document.body.removeChild(a)
      URL.revokeObjectURL(url)
    } catch (e) {
      setError(errorMessage(e, spec))
    } finally {
      setDownloading(null)
    }
  }

  /** Clicking a restricted document arms a confirmation first. */
  const handleSelect = (spec: DownloadSpec) => {
    if (spec.restricted && confirmKey !== spec.key) {
      setConfirmKey(spec.key)
      return
    }
    handleDownload(spec)
  }

  const busy = !!downloading
  const busySpec = ALL_DOWNLOADS.find(d => d.key === downloading)
  const primary = PROTOCOL_DOWNLOADS[0]

  return (
    <div ref={wrapperRef} className="relative">
      <div className="flex">
        <button
          onClick={() => !busy && handleDownload(primary)}
          disabled={busy}
          className="flex items-center gap-1.5 pl-3 pr-2 py-1.5 rounded-l-lg text-xs font-bold bg-cyan-600 text-white hover:bg-cyan-700 disabled:opacity-60 disabled:cursor-not-allowed transition-colors cursor-pointer"
        >
          {busy ? <SpinIcon /> : <DownloadIcon />}
          {busySpec ? `Exporting ${busySpec.format.toUpperCase()}…` : 'Export .docx'}
        </button>

        <button
          onClick={() => !busy && setOpen(o => !o)}
          disabled={busy}
          className="flex items-center px-1.5 py-1.5 rounded-r-lg text-xs font-bold bg-cyan-600 text-white hover:bg-cyan-700 disabled:opacity-60 disabled:cursor-not-allowed border-l border-cyan-500/50 transition-colors cursor-pointer"
          aria-label="More export options"
        >
          <svg
            width="10" height="10" viewBox="0 0 10 10" fill="none"
            className={`transition-transform duration-150 ${open ? 'rotate-180' : ''}`}
          >
            <path d="M2 3.5l3 3 3-3" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </button>
      </div>

      {error && (
        <p className="absolute right-0 top-full mt-1.5 z-30 max-w-[22rem] text-[11px] text-red-600 bg-red-50 border border-red-200 px-3 py-1.5 rounded-lg shadow-sm leading-relaxed">
          {error}
        </p>
      )}

      {open && !busy && (
        <div className="absolute right-0 top-full mt-1.5 w-72 bg-white border border-slate-200 rounded-xl shadow-xl z-30 overflow-hidden">
          <MenuHeading>Protocol document</MenuHeading>
          {PROTOCOL_DOWNLOADS.map(spec => (
            <MenuItem key={spec.key} spec={spec} onSelect={() => handleSelect(spec)} />
          ))}

          <MenuHeading>Study package</MenuHeading>
          {PACKAGE_DOWNLOADS.map(spec => (
            <div key={spec.key}>
              <MenuItem spec={spec} onSelect={() => handleSelect(spec)} />
              {confirmKey === spec.key && (
                <div className="px-4 py-3 bg-red-50 border-y border-red-200">
                  <p className="text-[11px] text-red-700 leading-relaxed">
                    This file contains the <span className="font-semibold">unblinded treatment
                    assignment for every subject</span>. File it in the restricted section of the
                    TMF — never in the protocol document or anywhere the study team can reach it.
                  </p>
                  <div className="flex gap-2 mt-2.5">
                    <button
                      onClick={() => handleDownload(spec)}
                      className="px-2.5 py-1 rounded-lg text-[11px] font-bold bg-red-600 text-white hover:bg-red-700 transition-colors cursor-pointer"
                    >
                      Download anyway
                    </button>
                    <button
                      onClick={() => setConfirmKey(null)}
                      className="px-2.5 py-1 rounded-lg text-[11px] font-semibold text-slate-600 border border-slate-200 bg-white hover:bg-slate-50 transition-colors cursor-pointer"
                    >
                      Cancel
                    </button>
                  </div>
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

/** A missing route means the backend build simply doesn't render that document
 *  yet — say so, rather than showing the reader a bare "Not Found". */
function errorMessage(e: unknown, spec: DownloadSpec): string {
  if (e instanceof HttpError && e.status === 404) {
    return `${spec.label} isn't available on the connected backend yet.`
  }
  if (e instanceof Error && e.message) return e.message
  return spec.format === 'pdf'
    ? 'PDF export failed — ensure reportlab is installed.'
    : 'Export failed. Please try again.'
}

function MenuHeading({ children }: { children: React.ReactNode }) {
  return (
    <p className="px-4 pt-2.5 pb-1 text-[10px] font-bold text-slate-400 uppercase tracking-widest bg-slate-50/70 border-b border-slate-100">
      {children}
    </p>
  )
}

function MenuItem({ spec, onSelect }: { spec: DownloadSpec; onSelect: () => void }) {
  return (
    <button
      onClick={onSelect}
      className="w-full text-left px-4 py-2.5 text-xs text-slate-700 hover:bg-slate-50 flex items-center gap-2.5 transition-colors font-medium cursor-pointer"
    >
      <FormatIcon format={spec.format} />
      <span className="flex-1 min-w-0 truncate">{spec.label}</span>
      {spec.restricted && (
        <span className="flex-shrink-0 text-[9px] font-bold text-red-700 bg-red-50 border border-red-200 px-1.5 py-0.5 rounded-md">
          RESTRICTED
        </span>
      )}
    </button>
  )
}

function DownloadIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 12 12" fill="none">
      <path d="M6 2v6M3.5 5.5L6 8l2.5-2.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M2 10h8" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  )
}

function SpinIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 12 12" fill="none" className="animate-spin">
      <circle cx="6" cy="6" r="5" stroke="rgba(255,255,255,0.3)" strokeWidth="2" />
      <path d="M6 1a5 5 0 015 5" stroke="white" strokeWidth="2" strokeLinecap="round" />
    </svg>
  )
}

function FormatIcon({ format }: { format: FileFormat }) {
  if (format === 'pdf') {
    return (
      <svg width="14" height="14" viewBox="0 0 14 14" fill="none" className="flex-shrink-0">
        <rect x="1.5" y="1.5" width="11" height="11" rx="1.5" stroke="#EF4444" strokeWidth="1.25" />
        <path d="M4 5.5h6M4 7.5h6M4 9.5h4" stroke="#EF4444" strokeWidth="1.25" strokeLinecap="round" />
      </svg>
    )
  }
  if (format === 'csv') {
    return (
      <svg width="14" height="14" viewBox="0 0 14 14" fill="none" className="flex-shrink-0">
        <rect x="1.5" y="1.5" width="11" height="11" rx="1.5" stroke="#059669" strokeWidth="1.25" />
        <path d="M1.5 5.5h11M5.5 5.5v7" stroke="#059669" strokeWidth="1.25" />
      </svg>
    )
  }
  return (
    <svg width="14" height="14" viewBox="0 0 14 14" fill="none" className="flex-shrink-0">
      <path d="M3 2h5.5l3.5 3.5V12a1 1 0 01-1 1H3a1 1 0 01-1-1V3a1 1 0 011-1z" stroke="#0891b2" strokeWidth="1.25" strokeLinejoin="round" />
      <path d="M8.5 2v4h3.5" stroke="#0891b2" strokeWidth="1.25" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}
