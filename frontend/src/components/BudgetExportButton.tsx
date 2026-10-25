import { useEffect, useState } from 'react'
import { api, HttpError } from '../api/client'
import { Button, Download } from '../ui'
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

/** Single-format download action for the budget's Word export. There is only
 *  one format, so this is a plain pill button rather than a menu. Mirrors
 *  ExportButton.tsx's fetch-a-blob-and-anchor-click mechanism exactly.
 *
 *  `primary` (default) is the editor toolbar's export: the black primary pill.
 *  `hero` is the echo inside the one-time "your budget is ready" banner, which
 *  sits right under that toolbar, so it is the outlined secondary pill. */
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

  return (
    <div className="relative">
      <Button
        variant={variant === 'hero' ? 'secondary' : 'primary'}
        onClick={handleDownload}
        loading={busy}
        leftIcon={<Download aria-hidden />}
        className={className}
      >
        {label}
      </Button>

      {error && (
        <p
          role="alert"
          className="absolute right-0 top-full z-30 mt-1.5 max-w-[22rem] rounded-input border border-[color:var(--bad-border)] bg-[var(--bad-bg)] px-3 py-2 text-small text-[color:var(--bad-fg)] shadow-pop"
        >
          {error}
        </p>
      )}
    </div>
  )
}
