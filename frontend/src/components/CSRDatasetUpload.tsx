import { useState } from 'react'
import { api, DatasetValidationError } from '../api/client'
import type { StudyReport } from '../types'

/** Step: download the fixed template, fill it in, upload the results file. On
 *  success the backend has run the deterministic analysis and returns the CSR
 *  with its locked `results`. Per-row validation errors are surfaced inline. */

function saveBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}

const KIND_DESC: Record<string, string> = {
  params: 'Subject-level PK parameters you have already computed (Cmax, AUC0-t, AUC0-∞ per subject/period). The platform runs the bioequivalence statistics.',
  nca: 'Raw concentration–time samples. The platform runs non-compartmental analysis (AUC/Cmax/λz) first, then the bioequivalence statistics.',
}

export default function CSRDatasetUpload({
  studyReport,
  onAnalyzed,
}: {
  studyReport: StudyReport
  onAnalyzed: (sr: StudyReport) => void
}) {
  const kind = studyReport.input_kind
  const [file, setFile] = useState<File | null>(null)
  const [lloq, setLloq] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [rowErrors, setRowErrors] = useState<string[]>([])

  async function download(fmt: 'csv' | 'xlsx') {
    try {
      const blob = await api.downloadCsrTemplate(kind, fmt)
      saveBlob(blob, `csr_${kind}_template.${fmt}`)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not download template')
    }
  }

  async function analyze() {
    if (!file) return
    setBusy(true)
    setError(null)
    setRowErrors([])
    try {
      const lloqNum = kind === 'nca' && lloq.trim() !== '' ? Number(lloq) : undefined
      const sr = await api.uploadStudyReportDataset(studyReport.id, file, lloqNum)
      onAnalyzed(sr)
    } catch (e) {
      if (e instanceof DatasetValidationError) {
        setError(e.message)
        setRowErrors(e.errors)
      } else {
        setError(e instanceof Error ? e.message : 'Upload failed')
      }
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="space-y-5">
      <div className="rounded-2xl border border-slate-200 bg-white p-5">
        <div className="flex items-center gap-2 mb-1">
          <h3 className="text-sm font-bold text-slate-800">1 · Get the template</h3>
          <span className="text-[10px] font-bold uppercase tracking-wide px-1.5 py-0.5 rounded-md bg-cyan-50 text-cyan-700 border border-cyan-200">
            {kind === 'params' ? 'Pre-computed parameters' : 'Raw concentration–time'}
          </span>
        </div>
        <p className="text-xs text-slate-500 leading-relaxed mb-3">{KIND_DESC[kind]}</p>
        <div className="flex gap-2">
          <button onClick={() => download('csv')} className="btn-ghost text-xs">Download CSV template</button>
          <button onClick={() => download('xlsx')} className="btn-ghost text-xs">Download Excel template</button>
        </div>
      </div>

      <div className="rounded-2xl border border-slate-200 bg-white p-5">
        <h3 className="text-sm font-bold text-slate-800 mb-3">2 · Upload your filled-in file</h3>

        {kind === 'nca' && (
          <div className="mb-4">
            <label className="block text-xs font-semibold text-slate-600 mb-1">
              LLOQ (lower limit of quantification) — optional
            </label>
            <input
              type="number"
              step="any"
              value={lloq}
              onChange={e => setLloq(e.target.value)}
              placeholder="e.g. 5"
              className="w-40 rounded-lg border border-slate-300 px-3 py-1.5 text-sm focus:border-cyan-500 focus:outline-none focus:ring-1 focus:ring-cyan-500"
            />
            <p className="text-[11px] text-slate-400 mt-1.5 leading-relaxed">
              Below-LLOQ samples: leave the concentration blank or enter <code className="text-slate-500">BLQ</code>. They are treated as
              missing (never zero) after the first measurable sample, and as 0 before it — the standard convention.
            </p>
          </div>
        )}

        <label className="flex items-center gap-3 cursor-pointer">
          <span className="btn-ghost text-xs">Choose file…</span>
          <input
            type="file"
            accept=".csv,.xlsx"
            onChange={e => { setFile(e.target.files?.[0] ?? null); setError(null); setRowErrors([]) }}
            className="hidden"
          />
          <span className="text-sm text-slate-500 truncate">{file ? file.name : 'No file selected (.csv or .xlsx)'}</span>
        </label>

        <button
          onClick={analyze}
          disabled={!file || busy}
          className="mt-4 inline-flex items-center gap-2 px-4 py-2.5 rounded-xl bg-cyan-600 hover:bg-cyan-700 text-white text-sm font-bold shadow-sm shadow-cyan-600/20 transition-colors cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {busy ? 'Analyzing…' : 'Upload & analyze'}
        </button>

        {error && (
          <div className="mt-4 rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
            <div className="font-semibold">{error}</div>
            {rowErrors.length > 0 && (
              <ul className="mt-2 space-y-0.5 text-xs list-disc pl-4 max-h-52 overflow-y-auto">
                {rowErrors.slice(0, 50).map((er, i) => <li key={i}>{er}</li>)}
                {rowErrors.length > 50 && <li>… and {rowErrors.length - 50} more</li>}
              </ul>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
