import { useRef, useState } from 'react'
import { api, DatasetValidationError } from '../api/client'
import type { StudyReport } from '../types'
import { Badge, Banner, Button, Card, CardHeader, Download, Field, FileText, Input } from '../ui'

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
  const fileInputRef = useRef<HTMLInputElement>(null)
  const lloqInvalid = kind === 'nca' && lloq.trim() !== '' && !(Number.isFinite(Number(lloq)) && Number(lloq) > 0)

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
    if (lloqInvalid) {
      setError('LLOQ must be a positive number.')
      return
    }
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
      <Card>
        <CardHeader
          level={2}
          title="1 · Get the template"
          badge={<Badge variant="info">{kind === 'params' ? 'Pre-computed parameters' : 'Raw concentration–time'}</Badge>}
          subtitle={KIND_DESC[kind]}
        />
        <div className="mt-4 flex flex-wrap gap-2">
          <Button variant="secondary" size="sm" leftIcon={<Download aria-hidden />} onClick={() => download('csv')}>
            Download CSV template
          </Button>
          <Button variant="secondary" size="sm" leftIcon={<Download aria-hidden />} onClick={() => download('xlsx')}>
            Download Excel template
          </Button>
        </div>
      </Card>

      <Card>
        <CardHeader level={2} title="2 · Upload your filled-in file" />

        {kind === 'nca' && (
          <div className="mt-4">
            <Field
              label="LLOQ (lower limit of quantification), optional"
              error={lloqInvalid ? 'LLOQ must be a positive number.' : undefined}
              hint={
                <>
                  Below-LLOQ samples: leave the concentration blank or enter <code className="font-mono">BLQ</code>. They are treated as
                  missing (never zero) after the first measurable sample, and as 0 before it, the standard convention.
                </>
              }
              className="max-w-md"
            >
              <Input
                type="number"
                step="any"
                min="0.0000001"
                value={lloq}
                onChange={e => setLloq(e.target.value)}
                placeholder="e.g. 5"
                className="w-40"
              />
            </Field>
          </div>
        )}

        <div className="mt-4 flex flex-wrap items-center gap-3">
          <input
            ref={fileInputRef}
            type="file"
            accept=".csv,.xlsx"
            aria-label="Choose results file (.csv or .xlsx)"
            onChange={e => { setFile(e.target.files?.[0] ?? null); setError(null); setRowErrors([]) }}
            tabIndex={-1}
            className="hidden"
          />
          <Button
            variant="secondary"
            size="sm"
            leftIcon={<FileText aria-hidden />}
            onClick={() => fileInputRef.current?.click()}
          >
            Choose file…
          </Button>
          <span className="min-w-0 truncate text-body text-fg-muted">{file ? file.name : 'No file selected (.csv or .xlsx)'}</span>
        </div>

        <div className="mt-4">
          <Button onClick={analyze} loading={busy} disabled={!file || lloqInvalid}>
            {busy ? 'Analyzing…' : 'Upload & analyze'}
          </Button>
        </div>

        {error ? (
          <Banner tone="danger" title={error} className="mt-4">
            {rowErrors.length > 0 ? (
              <ul className="mt-1 max-h-52 list-disc space-y-0.5 overflow-y-auto pl-4 text-small">
                {rowErrors.slice(0, 50).map((er, i) => <li key={i}>{er}</li>)}
                {rowErrors.length > 50 && <li>… and {rowErrors.length - 50} more</li>}
              </ul>
            ) : null}
          </Banner>
        ) : null}
      </Card>
    </div>
  )
}
