import { useEffect, useState } from 'react'
import { api } from '../api/client'
import { CSR_SECTION_ORDER, CSR_SECTION_TITLES } from '../types'
import type { CsrInputKind, CsrQCReport, ProtocolSummary, StudyReport } from '../types'
import { Badge, Banner, Button, Card, CardHeader, Check, ChevronLeft, ChevronRight, Download, RefreshCw, Spinner, cx } from '../ui'
import CSRDatasetUpload from './CSRDatasetUpload'
import CSRResultsReview from './CSRResultsReview'

type Phase = 'loading' | 'start' | 'upload' | 'review' | 'report'

const sleep = (ms: number) => new Promise(r => setTimeout(r, ms))

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

function hasResults(sr: StudyReport | null): boolean {
  return !!sr?.results?.be && Object.keys(sr.results.be).length > 0
}
function hasNarrative(sr: StudyReport | null): boolean {
  return !!sr?.sections && Object.keys(sr.sections).length > 0
}

export default function CSRView({ protocol, onBack }: { protocol: ProtocolSummary; onBack: () => void }) {
  const [phase, setPhase] = useState<Phase>('loading')
  const [studyReport, setStudyReport] = useState<StudyReport | null>(null)
  const [inputKind, setInputKind] = useState<CsrInputKind>('params')
  const [loadError, setLoadError] = useState<string | null>(null)
  const [creating, setCreating] = useState(false)

  const [genBusy, setGenBusy] = useState(false)
  const [genError, setGenError] = useState<string | null>(null)
  const [qc, setQc] = useState<CsrQCReport | null>(null)
  const [exporting, setExporting] = useState<'docx' | 'pdf' | null>(null)

  // On mount, resume any existing CSR for this protocol.
  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const list = await api.listStudyReports(protocol.id)
        if (cancelled) return
        if (list.length === 0) {
          setPhase('start')
          return
        }
        const full = await api.getStudyReport(list[0].id)
        if (cancelled) return
        setStudyReport(full)
        setInputKind(full.input_kind)
        if (!hasResults(full)) setPhase('upload')
        else if (hasNarrative(full) && full.generation_status === 'complete') setPhase('report')
        else setPhase('review')
      } catch (e) {
        if (!cancelled) {
          setLoadError(e instanceof Error ? e.message : 'Failed to load study report')
          setPhase('start')
        }
      }
    })()
    return () => { cancelled = true }
  }, [protocol.id])

  async function startCreate() {
    setCreating(true)
    setLoadError(null)
    try {
      const sr = await api.createStudyReport(protocol.id, inputKind)
      setStudyReport(sr)
      setPhase('upload')
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : 'Could not start the study report')
    } finally {
      setCreating(false)
    }
  }

  function onAnalyzed(sr: StudyReport) {
    setStudyReport(sr)
    setPhase('review')
  }

  async function pollUntilDone(id: string): Promise<StudyReport> {
    // 600 x 3s = ~30 min, matching GenerateButton. The server has no matching
    // timeout, so a client ceiling below the real generation time reports a
    // failure for work that actually succeeded.
    for (let i = 0; i < 600; i++) {
      await sleep(3000)
      let p: StudyReport
      try { p = await api.getStudyReport(id) } catch { continue }
      if (p.generation_status === 'failed') throw new Error(p.generation_error || 'Generation failed.')
      if (p.generation_status === 'complete' || p.generation_status == null) return p
    }
    throw new Error('Generation is taking longer than expected. Check back shortly.')
  }

  async function generate() {
    if (!studyReport) return
    setGenBusy(true)
    setGenError(null)
    try {
      let sr = await api.generateStudyReport(studyReport.id)
      if (sr.generation_status === 'generating') sr = await pollUntilDone(sr.id)
      setStudyReport(sr)
      setPhase('report')
    } catch (e) {
      setGenError(e instanceof Error ? e.message : 'Report generation failed')
    } finally {
      setGenBusy(false)
    }
  }

  // Load QC once we reach the report (and after analysis).
  useEffect(() => {
    if (!studyReport || !hasResults(studyReport)) return
    let cancelled = false
    api.getStudyReportQC(studyReport.id)
      .then(r => { if (!cancelled) setQc(r) })
      .catch(() => { /* QC is supplementary */ })
    return () => { cancelled = true }
  }, [studyReport?.id, studyReport?.results, phase])

  async function doExport(fmt: 'docx' | 'pdf') {
    if (!studyReport) return
    setExporting(fmt)
    try {
      const blob = fmt === 'docx' ? await api.exportCsrDocx(studyReport.id) : await api.exportCsrPdf(studyReport.id)
      saveBlob(blob, `CSR_${protocol.drug_inn.replace(/\s+/g, '_')}_v${studyReport.version}.${fmt}`)
    } catch (e) {
      setGenError(e instanceof Error ? e.message : `Export (${fmt}) failed`)
    } finally {
      setExporting(null)
    }
  }

  const steps: { key: Phase; label: string }[] = [
    { key: 'upload', label: 'Upload data' },
    { key: 'review', label: 'Review results' },
    { key: 'report', label: 'Generate CSR' },
  ]
  const activeIdx = phase === 'start' || phase === 'loading' ? 0 : steps.findIndex(s => s.key === phase)

  return (
    <div className="mx-auto w-full max-w-4xl px-4 py-8 sm:px-8 sm:py-10">
      {/* Header */}
      <div className="mb-6 flex flex-wrap items-start gap-x-3 gap-y-2">
        <Button variant="secondary" size="sm" onClick={onBack} leftIcon={<ChevronLeft aria-hidden />}>
          Protocols
        </Button>
        <div className="min-w-0 flex-1 basis-64">
          <div className="flex flex-wrap items-center gap-2">
            <h1 className="min-w-0 truncate text-h1 text-fg">
              <span className="capitalize">{protocol.drug_inn}</span> · Clinical study report
            </h1>
            <Badge variant="neutral">CSR · ICH E3</Badge>
          </div>
          <p className="mt-1 text-body text-fg-muted">Post-study bioequivalence analysis, then the Clinical Study Report (CSR).</p>
        </div>
      </div>

      {/* Step indicator */}
      {phase !== 'loading' && (
        <ol aria-label="Progress" className="mb-6 flex flex-wrap items-center gap-2">
          {steps.map((s, i) => {
            const done = i < activeIdx
            const current = i === activeIdx
            return (
              <li key={s.key} aria-current={current ? 'step' : undefined} className="flex items-center gap-2">
                <Badge
                  variant={current ? 'solid' : done ? 'success' : 'neutral'}
                  icon={done ? <Check aria-hidden className="h-3.5 w-3.5" /> : undefined}
                  className="h-7 px-3 text-small"
                >
                  {i + 1}. {s.label}
                  {done ? <span className="sr-only"> (done)</span> : null}
                </Badge>
                {i < steps.length - 1 ? <ChevronRight aria-hidden className="h-4 w-4 text-ink-300" /> : null}
              </li>
            )
          })}
        </ol>
      )}

      {loadError ? (
        <Banner tone="danger" className="mb-4">
          {loadError}
        </Banner>
      ) : null}

      {phase === 'loading' && !loadError && (
        <div className="space-y-3" role="status" aria-busy="true" aria-label="Loading study report">
          {[0, 1].map(i => <div key={i} className="h-24 animate-pulse rounded-card border border-line bg-surface-muted" />)}
        </div>
      )}

      {/* Start: pick input kind */}
      {phase === 'start' && (
        <Card padding="lg">
          <fieldset>
            <legend className="text-h3 text-fg">What are you uploading?</legend>
            <p className="mt-1 text-small text-fg-muted">
              This determines the template and whether the platform runs non-compartmental analysis for you.
            </p>
            <div className="mt-4 space-y-2.5">
              {([
                ['params', 'Pre-computed PK parameters', 'You already have per-subject Cmax / AUC (from Phoenix, R, etc.). We run the bioequivalence statistics.'],
                ['nca', 'Raw concentration–time data', 'You have the concentration samples. We run non-compartmental analysis (AUC/Cmax/λz) first, then the statistics.'],
              ] as [CsrInputKind, string, string][]).map(([k, title, desc]) => (
                <label
                  key={k}
                  className={cx(
                    'flex cursor-pointer items-start gap-3 rounded-input border p-4 transition-colors duration-fast',
                    inputKind === k
                      ? 'border-ink-950 bg-surface-muted ring-1 ring-ink-950'
                      : 'border-line hover:border-line-strong',
                  )}
                >
                  <input
                    type="radio"
                    name="inputKind"
                    checked={inputKind === k}
                    onChange={() => setInputKind(k)}
                    className="mt-1 accent-ink-950"
                  />
                  <div>
                    <div className="text-body font-semibold text-fg">{title}</div>
                    <div className="mt-0.5 text-small text-fg-muted">{desc}</div>
                  </div>
                </label>
              ))}
            </div>
          </fieldset>
          <div className="mt-5">
            <Button onClick={startCreate} loading={creating}>
              {creating ? 'Starting…' : 'Continue'}
            </Button>
          </div>
        </Card>
      )}

      {/* Upload */}
      {phase === 'upload' && studyReport && (
        <CSRDatasetUpload studyReport={studyReport} onAnalyzed={onAnalyzed} />
      )}

      {/* Review results, then generate the narrative */}
      {phase === 'review' && studyReport?.results && (
        <div className="space-y-6">
          <CSRResultsReview results={studyReport.results} />
          <Card>
            <CardHeader
              level={2}
              title="Generate the Clinical Study Report (CSR)"
              subtitle={
                <>
                  The numbers above are locked. Generating the CSR writes the full ICH E3 narrative <em>around</em> them; it never
                  changes a figure.
                </>
              }
            />
            {genError ? (
              <Banner tone="danger" className="mt-3">
                {genError}
              </Banner>
            ) : null}
            {genBusy ? (
              <Banner tone="info" className="mt-3" title="Generating your Clinical Study Report…">
                <span className="inline-flex items-start gap-2">
                  <Spinner size={14} className="mt-1" />
                  <span>The AI is drafting each ICH E3 section around your locked results. This can take a minute or two.</span>
                </span>
              </Banner>
            ) : null}
            <div className="mt-4 flex flex-wrap items-center gap-2">
              <Button onClick={generate} loading={genBusy}>
                {genBusy ? 'Generating CSR…' : 'Generate Clinical Study Report'}
              </Button>
              <Button variant="ghost" onClick={() => setPhase('upload')} disabled={genBusy}>
                Re-upload data
              </Button>
            </div>
          </Card>
        </div>
      )}

      {/* Final report */}
      {phase === 'report' && studyReport?.results && (
        <div className="space-y-6">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <h2 className="text-h2 text-fg">Clinical Study Report: draft</h2>
              <p className="text-small text-fg-subtle">ICH E3 structure · for qualified review before use</p>
            </div>
            <div className="flex items-center gap-2">
              <Button
                variant="secondary"
                onClick={() => doExport('docx')}
                loading={exporting === 'docx'}
                disabled={exporting !== null}
                leftIcon={<Download aria-hidden />}
              >
                {exporting === 'docx' ? 'Exporting…' : 'Export CSR (.docx)'}
              </Button>
              <Button
                variant="secondary"
                onClick={() => doExport('pdf')}
                loading={exporting === 'pdf'}
                disabled={exporting !== null}
                leftIcon={<Download aria-hidden />}
              >
                {exporting === 'pdf' ? 'Exporting…' : 'Export CSR (.pdf)'}
              </Button>
            </div>
          </div>
          {genError ? <Banner tone="danger">{genError}</Banner> : null}

          <CSRResultsReview results={studyReport.results} />

          {qc && (
            <Banner tone={qc.submission_ready ? 'success' : 'warning'} title="Quality check">
              {qc.submission_ready ? 'No blocking issues. Requires expert sign-off.' : `${qc.counts.fail || 0} blocking issue(s) to resolve.`}
              <span className="text-fg-subtle"> · {qc.counts.pass || 0} pass · {qc.counts.warning || 0} warning · {qc.counts.fail || 0} fail</span>
            </Banner>
          )}

          {/* Narrative sections (read-only) */}
          <Card padding="lg" className="space-y-5">
            <h2 className="text-h2 text-fg">ICH E3 narrative</h2>
            {CSR_SECTION_ORDER.filter(k => k !== 'title').map((key, i) => {
              const html = studyReport.sections?.[key]
              if (!html) return null
              return (
                <section key={key}>
                  <h3 className="mb-1.5 text-h3 text-fg">{i + 1}. {CSR_SECTION_TITLES[key]}</h3>
                  <div
                    className="text-body text-fg-muted [&_p]:mb-2 [&_h3]:mb-1 [&_h3]:mt-3 [&_h3]:font-semibold [&_h3]:text-fg [&_ul]:list-disc [&_ul]:space-y-1 [&_ul]:pl-5 [&_ol]:list-decimal [&_ol]:pl-5 [&_table]:w-full [&_table]:text-small [&_td]:border [&_td]:border-line [&_td]:px-2 [&_td]:py-1 [&_th]:border [&_th]:border-line [&_th]:bg-surface-muted [&_th]:px-2 [&_th]:py-1"
                    dangerouslySetInnerHTML={{ __html: html }}
                  />
                </section>
              )
            })}
          </Card>

          <Button variant="ghost" onClick={() => setPhase('review')} leftIcon={<RefreshCw aria-hidden />}>
            Re-generate narrative
          </Button>
        </div>
      )}
    </div>
  )
}
