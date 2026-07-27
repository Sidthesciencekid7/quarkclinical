import { useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import type { Drug, GeneratedProtocol } from '../types'

interface Props {
  protocol: GeneratedProtocol
  drug: Drug
}

type Format = 'docx' | 'pdf'

const FORMAT_LABEL: Record<Format, string> = {
  docx: 'Export .docx',
  pdf: 'Export .pdf',
}

export default function ExportButton({ protocol, drug }: Props) {
  const [open, setOpen] = useState(false)
  const [exporting, setExporting] = useState<Format | null>(null)
  const [error, setError] = useState<string | null>(null)
  const wrapperRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (wrapperRef.current && !wrapperRef.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [])

  useEffect(() => {
    if (!error) return
    const t = setTimeout(() => setError(null), 5000)
    return () => clearTimeout(t)
  }, [error])

  const handleExport = async (format: Format) => {
    setOpen(false)
    setError(null)
    setExporting(format)
    try {
      const blob = format === 'docx' ? await api.exportDocx(protocol.id) : await api.exportPdf(protocol.id)
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `protocol_${drug.inn}_v${protocol.version}.${format}`
      document.body.appendChild(a)
      a.click()
      document.body.removeChild(a)
      URL.revokeObjectURL(url)
    } catch {
      setError(format === 'pdf'
        ? 'PDF export failed — ensure reportlab is installed.'
        : 'DOCX export failed. Please try again.')
    } finally {
      setExporting(null)
    }
  }

  const busy = !!exporting

  return (
    <div ref={wrapperRef} className="relative">
      <div className="flex">
        <button
          onClick={() => !busy && handleExport('docx')}
          disabled={busy}
          className="flex items-center gap-1.5 pl-3 pr-2 py-1.5 rounded-l-lg text-xs font-bold bg-cyan-600 text-white hover:bg-cyan-700 disabled:opacity-60 disabled:cursor-not-allowed transition-colors cursor-pointer"
        >
          {busy ? <SpinIcon /> : <DownloadIcon />}
          {exporting ? `Exporting ${exporting.toUpperCase()}…` : 'Export .docx'}
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
        <p className="absolute right-0 top-full mt-1.5 z-30 text-[11px] text-red-600 bg-red-50 border border-red-200 px-3 py-1.5 rounded-lg whitespace-nowrap shadow-sm">
          {error}
        </p>
      )}

      {open && !busy && (
        <div className="absolute right-0 top-full mt-1.5 w-44 bg-white border border-slate-200 rounded-xl shadow-xl z-30 overflow-hidden">
          {(['docx', 'pdf'] as Format[]).map(fmt => (
            <button
              key={fmt}
              onClick={() => handleExport(fmt)}
              className="w-full text-left px-4 py-2.5 text-xs text-slate-700 hover:bg-slate-50 flex items-center gap-2.5 transition-colors font-medium cursor-pointer"
            >
              <FormatIcon format={fmt} />
              {FORMAT_LABEL[fmt]}
            </button>
          ))}
        </div>
      )}
    </div>
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

function FormatIcon({ format }: { format: Format }) {
  if (format === 'pdf') {
    return (
      <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
        <rect x="1.5" y="1.5" width="11" height="11" rx="1.5" stroke="#EF4444" strokeWidth="1.25" />
        <path d="M4 5.5h6M4 7.5h6M4 9.5h4" stroke="#EF4444" strokeWidth="1.25" strokeLinecap="round" />
      </svg>
    )
  }
  return (
    <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
      <path d="M3 2h5.5l3.5 3.5V12a1 1 0 01-1 1H3a1 1 0 01-1-1V3a1 1 0 011-1z" stroke="#0891b2" strokeWidth="1.25" strokeLinejoin="round" />
      <path d="M8.5 2v4h3.5" stroke="#0891b2" strokeWidth="1.25" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}
