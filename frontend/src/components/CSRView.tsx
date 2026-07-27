import { useEffect, useState } from 'react'
import { api } from '../api/client'
import { CSR_SECTION_ORDER, CSR_SECTION_TITLES } from '../types'
import type { CsrInputKind, CsrQCReport, ProtocolSummary, StudyReport } from '../types'
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
        if (!cancelled) setLoadError(e instanceof Error ? e.message : 'Failed to load study report')
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
    for (let i = 0; i < 200; i++) {
      await sleep(3000)
      let p: StudyReport
      try { p = await api.getStudyReport(id) } catch { continue }
      if (p.generation_status === 'failed') throw new Error(p.generation_error || 'Generation failed.')
      if (p.generation_status === 'complete' || p.generation_status == null) return p
    }
    throw new Error('Generation is taking longer than expected — check back shortly.')
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
    <div className="app-canvas min-h-[calc(100vh-57px)]">
      <div className="max-w-4xl mx-auto px-5 py-8">
        {/* Header */}
        <div className="flex items-center gap-3 mb-6 flex-wrap">
          <button onClick={onBack} className="btn-ghost text-xs">
            <svg width="13" height="13" viewBox="0 0 14 14" fill="none"><path d="M9 3L4 7l5 4" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" /></svg>
            My Reports
          </button>
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2 flex-wrap">
              <h1 className="text-xl font-extrabold text-slate-900 tracking-tight capitalize truncate">
                <span className="capitalize">{protocol.drug_inn}</span> — Clinical Study Report
              </h1>
              <span className="text-[10px] font-bold uppercase tracking-wide px-2 py-0.5 rounded-md bg-teal-50 text-teal-700 border border-teal-200">
                CSR · ICH E3
              </span>
            </div>
            <p className="text-xs text-slate-500">Post-study bioequivalence analysis → Clinical Study Report (CSR)</p>
          </div>
        </div>

        {/* Step indicator */}
        {phase !== 'loading' && (
          <div className="flex items-center gap-2 mb-6 text-xs font-semibold">
            {steps.map((s, i) => (
              <div key={s.key} className="flex items-center gap-2">
                <span className={`px-2.5 py-1 rounded-lg border ${i === activeIdx ? 'bg-cyan-50 text-cyan-700 border-cyan-200' : i < activeIdx ? 'bg-emerald-50 text-emerald-700 border-emerald-200' : 'bg-slate-50 text-slate-400 border-slate-200'}`}>
                  {i + 1}. {s.label}
                </span>
                {i < steps.length - 1 && <span className="text-slate-300">›</span>}
              </div>
            ))}
          </div>
        )}

        {loadError && (
          <div className="mb-4 rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">{loadError}</div>
        )}

        {phase === 'loading' && (
          <div className="space-y-3">{[0, 1].map(i => <div key={i} className="h-24 rounded-2xl border border-slate-200 bg-white/60 animate-pulse" />)}</div>
        )}

        {/* Start: pick input kind */}
        {phase === 'start' && (
          <div className="rounded-2xl border border-slate-200 bg-white p-6">
            <h3 className="text-sm font-bold text-slate-800 mb-1">What are you uploading?</h3>
            <p className="text-xs text-slate-500 mb-4">This determines the template and whether the platform runs non-compartmental analysis for you.</p>
            <div className="space-y-2.5">
              {([
                ['params', 'Pre-computed PK parameters', 'You already have per-subject Cmax / AUC (from Phoenix, R, etc.). We run the bioequivalence statistics.'],
                ['nca', 'Raw concentration–time data', 'You have the concentration samples. We run non-compartmental analysis (AUC/Cmax/λz) first, then the statistics.'],
              ] as [CsrInputKind, string, string][]).map(([k, title, desc]) => (
                <label key={k} className={`flex items-start gap-3 rounded-xl border p-4 cursor-pointer transition-colors ${inputKind === k ? 'border-cyan-400 bg-cyan-50/60 ring-1 ring-cyan-300' : 'border-slate-200 hover:border-slate-300'}`}>
                  <input type="radio" name="inputKind" checked={inputKind === k} onChange={() => setInputKind(k)} className="mt-1 accent-cyan-600" />
                  <div>
                    <div className="text-sm font-bold text-slate-800">{title}</div>
                    <div className="text-xs text-slate-500 mt-0.5 leading-relaxed">{desc}</div>
                  </div>
                </label>
              ))}
            </div>
            <button onClick={startCreate} disabled={creating} className="mt-5 inline-flex items-center gap-2 px-4 py-2.5 rounded-xl bg-cyan-600 hover:bg-cyan-700 text-white text-sm font-bold shadow-sm shadow-cyan-600/20 transition-colors cursor-pointer disabled:opacity-50">
              {creating ? 'Starting…' : 'Continue'}
            </button>
          </div>
        )}

        {/* Upload */}
        {phase === 'upload' && studyReport && (
          <CSRDatasetUpload studyReport={studyReport} onAnalyzed={onAnalyzed} />
        )}

        {/* Review results → generate narrative */}
        {phase === 'review' && studyReport?.results && (
          <div className="space-y-6">
            <CSRResultsReview results={studyReport.results} />
            <div className="rounded-2xl border border-slate-200 bg-white p-5">
              <h3 className="text-sm font-bold text-slate-800">Generate the Clinical Study Report (CSR)</h3>
              <p className="text-xs text-slate-500 mt-1 leading-relaxed">
                The numbers above are locked. Generating the CSR writes the full ICH E3 narrative <em>around</em> them — it never changes a figure.
              </p>
              {genError && <div className="mt-3 rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">{genError}</div>}
              {genBusy && (
                <div className="mt-3 flex items-center gap-3 rounded-xl border border-cyan-200 bg-cyan-50 px-4 py-3 text-sm text-cyan-800">
                  <svg width="16" height="16" viewBox="0 0 16 16" fill="none" className="animate-spin flex-shrink-0"><circle cx="8" cy="8" r="6" stroke="#a5f3fc" strokeWidth="2.5" /><path d="M8 2a6 6 0 016 6" stroke="#0891b2" strokeWidth="2.5" strokeLinecap="round" /></svg>
                  <span><strong>Generating your Clinical Study Report…</strong> the AI is drafting each ICH E3 section around your locked results. This can take a minute or two.</span>
                </div>
              )}
              <div className="mt-4 flex items-center gap-2">
                <button onClick={generate} disabled={genBusy} className="inline-flex items-center gap-2 px-4 py-2.5 rounded-xl bg-cyan-600 hover:bg-cyan-700 text-white text-sm font-bold shadow-sm shadow-cyan-600/20 transition-colors cursor-pointer disabled:opacity-50">
                  {genBusy ? 'Generating CSR…' : 'Generate Clinical Study Report'}
                </button>
                <button onClick={() => setPhase('upload')} disabled={genBusy} className="btn-ghost text-xs disabled:opacity-50">Re-upload data</button>
              </div>
            </div>
          </div>
        )}

        {/* Final report */}
        {phase === 'report' && studyReport?.results && (
          <div className="space-y-6">
            <div className="flex items-center justify-between gap-3 flex-wrap">
              <div>
                <h2 className="text-base font-extrabold text-slate-900">Clinical Study Report — Draft</h2>
                <p className="text-[11px] text-slate-400">ICH E3 structure · for qualified review before use</p>
              </div>
              <div className="flex items-center gap-2">
                <button onClick={() => doExport('docx')} disabled={exporting !== null} className="inline-flex items-center gap-1.5 px-3 py-2 rounded-lg border border-slate-200 text-slate-600 text-xs font-semibold hover:bg-slate-50 cursor-pointer disabled:opacity-50">
                  {exporting === 'docx' ? 'Exporting…' : 'Export CSR (.docx)'}
                </button>
                <button onClick={() => doExport('pdf')} disabled={exporting !== null} className="inline-flex items-center gap-1.5 px-3 py-2 rounded-lg border border-slate-200 text-slate-600 text-xs font-semibold hover:bg-slate-50 cursor-pointer disabled:opacity-50">
                  {exporting === 'pdf' ? 'Exporting…' : 'Export CSR (.pdf)'}
                </button>
              </div>
            </div>
            {genError && <div className="rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">{genError}</div>}

            <CSRResultsReview results={studyReport.results} />

            {qc && (
              <div className={`rounded-xl border px-4 py-3 text-sm ${qc.submission_ready ? 'border-emerald-200 bg-emerald-50 text-emerald-800' : 'border-amber-200 bg-amber-50 text-amber-800'}`}>
                <span className="font-semibold">QC: </span>
                {qc.submission_ready ? 'No blocking issues — requires expert sign-off.' : `${qc.counts.fail || 0} blocking issue(s) to resolve.`}
                <span className="opacity-70"> · {qc.counts.pass || 0} pass · {qc.counts.warning || 0} warning · {qc.counts.fail || 0} fail</span>
              </div>
            )}

            {/* Narrative sections (read-only) */}
            <div className="rounded-2xl border border-slate-200 bg-white p-6 space-y-5">
              <h2 className="text-lg font-extrabold text-slate-900">ICH E3 Narrative</h2>
              {CSR_SECTION_ORDER.filter(k => k !== 'title').map((key, i) => {
                const html = studyReport.sections?.[key]
                if (!html) return null
                return (
                  <section key={key}>
                    <h3 className="text-sm font-bold text-cyan-800 mb-1.5">{i + 1}. {CSR_SECTION_TITLES[key]}</h3>
                    <div
                      className="text-sm text-slate-700 leading-relaxed [&_p]:mb-2 [&_h3]:font-bold [&_h3]:text-slate-800 [&_h3]:mt-3 [&_h3]:mb-1 [&_ul]:list-disc [&_ul]:pl-5 [&_ul]:space-y-1 [&_ol]:list-decimal [&_ol]:pl-5 [&_table]:w-full [&_table]:text-xs [&_td]:border [&_td]:border-slate-200 [&_td]:px-2 [&_td]:py-1 [&_th]:border [&_th]:border-slate-200 [&_th]:px-2 [&_th]:py-1 [&_th]:bg-slate-50"
                      dangerouslySetInnerHTML={{ __html: html }}
                    />
                  </section>
                )
              })}
            </div>

            <button onClick={() => setPhase('review')} className="btn-ghost text-xs">Re-generate narrative</button>
          </div>
        )}
      </div>
    </div>
  )
}
