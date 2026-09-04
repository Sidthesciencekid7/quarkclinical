import { useEffect, useState } from 'react'
import { api, HttpError } from '../api/client'
import type { Budget, BudgetSummary } from '../types'

interface Props {
  budget: Budget
  variant?: 'primary' | 'hero'
  className?: string
}

function slug(s: string): string {
  return s.trim().replace(/\s+/g, '_').replace(/[^A-Za-z0-9_\-]/g, '') || 'budget'
}

function filenameFor(b: { name: string; drug_inn: string | null }): string {
  return `budget_${slug(b.drug_inn ?? b.name)}_${new Date().toISOString().slice(0, 10)}.docx`
}

function messageFor(e: unknown): string {
  if (e instanceof HttpError && e.status === 404) {
    return "The Word export isn't available on the connected backend yet."
  }
  if (e instanceof Error && e.message) return e.message
  return 'Export failed. Please try again.'
}

/** The one place the download-and-filename logic lives, so BudgetExportButton
 *  and BudgetList's row action never disagree. EXACT anchor-click pattern
 *  from ExportButton.tsx / CSRView.tsx's saveBlob — no new tab, no
 *  window.location. */
export async function downloadBudgetDocx(budget: Pick<Budget | BudgetSummary, 'id' | 'name' | 'drug_inn'>): Promise<void> {
  const blob = await api.exportBudgetDocx(budget.id)
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filenameFor(budget)
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
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

/** Single-format download action for the budget's Word export — no dropdown,
 *  this IS the primary action. Mirrors ExportButton.tsx's fetch-a-blob-and-
 *  anchor-click mechanism exactly. */
export default function BudgetExportButton({ budget, variant = 'primary', className }: Props) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!error) return
    const t = setTimeout(() => setError(null), 6000)
    return () => clearTimeout(t)
  }, [error])

  async function handleDownload() {
    if (busy) return
    setBusy(true)
    setError(null)
    try {
      await downloadBudgetDocx(budget)
    } catch (e) {
      setError(messageFor(e))
    } finally {
      setBusy(false)
    }
  }

  const label = variant === 'hero'
    ? (busy ? 'Building .docx…' : 'Download Word document')
    : (busy ? 'Building .docx…' : 'Download Word (.docx)')

  const buttonClassName = variant === 'hero'
    ? `btn-primary ${className ?? ''}`
    : `flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-bold bg-cyan-600 text-white hover:bg-cyan-700 disabled:opacity-60 disabled:cursor-not-allowed transition-colors cursor-pointer ${className ?? ''}`

  return (
    <div className="relative">
      <button onClick={handleDownload} disabled={busy} className={buttonClassName}>
        {busy ? <SpinIcon /> : <DownloadIcon />}
        {label}
      </button>

      {error && (
        <p className="absolute right-0 top-full mt-1.5 z-30 max-w-[22rem] text-[11px] text-red-600 bg-red-50 border border-red-200 px-3 py-1.5 rounded-lg shadow-sm leading-relaxed">
          {error}
        </p>
      )}
    </div>
  )
}
