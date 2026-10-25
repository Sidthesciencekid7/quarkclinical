import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { CsrResults, ProtocolSummary, StudyReport } from '../../../types'

const h = vi.hoisted(() => {
  class DatasetValidationError extends Error {
    errors: string[]
    constructor(message: string, errors: string[]) {
      super(message)
      this.errors = errors
    }
  }
  return {
    DatasetValidationError,
    api: {
      listStudyReports: vi.fn(),
      getStudyReport: vi.fn(),
      createStudyReport: vi.fn(),
      uploadStudyReportDataset: vi.fn(),
      downloadCsrTemplate: vi.fn(),
      generateStudyReport: vi.fn(),
      getStudyReportQC: vi.fn(),
      exportCsrDocx: vi.fn(),
      exportCsrPdf: vi.fn(),
    },
  }
})
vi.mock('../../../api/client', () => ({ api: h.api, DatasetValidationError: h.DatasetValidationError }))

import CSRView from '../../../components/CSRView'
import CSRDatasetUpload from '../../../components/CSRDatasetUpload'
import CSRResultsReview from '../../../components/CSRResultsReview'

const PROTOCOL: ProtocolSummary = {
  id: 'p1',
  drug_id: 'd1',
  drug_inn: 'ibuprofen',
  drug_brand_names: [],
  study_type: 'BE',
  guideline_version: 'FDA-BE-2014',
  guideline_outdated: false,
  version: 1,
  status: 'draft',
  section_count: 17,
  created_at: '2026-10-01T10:00:00Z',
}

function report(over: Partial<StudyReport> = {}): StudyReport {
  return {
    id: 'sr1',
    protocol_id: 'p1',
    org_id: 'o1',
    input_kind: 'params',
    version: 1,
    created_by: 'u1',
    created_at: '2026-10-01T10:00:00Z',
    ...over,
  }
}

const RESULTS: CsrResults = {
  design_key: 'crossover',
  co_primary: ['cmax', 'auc0inf'],
  verdict: 'bioequivalent',
  be: {
    cmax: { metric: 'cmax', n: 24, point_estimate_pct: 98.31, ci_lower_pct: 91.2, ci_upper_pct: 105.9, cv_intra_pct: 14.2, within_be_limits: true, lower_limit_pct: 80, upper_limit_pct: 125 },
    auc0inf: { metric: 'auc0inf', n: 24, point_estimate_pct: 100.2, ci_lower_pct: 94.1, ci_upper_pct: 106.7, cv_intra_pct: 11.1, within_be_limits: false, lower_limit_pct: 80, upper_limit_pct: 125 },
  },
  disposition: { n_subjects: 24, treatments: ['T', 'R'], periods: [1, 2], sequences: ['TR', 'RT'] },
}

beforeEach(() => {
  for (const fn of Object.values(h.api)) fn.mockReset()
})

describe('CSRView (outer element is not a <main>)', () => {
  it('starts at the input-kind choice, creates the report with the chosen kind, and moves to the upload step', async () => {
    h.api.listStudyReports.mockResolvedValue([])
    h.api.createStudyReport.mockResolvedValue(report({ input_kind: 'nca' }))
    const { container } = render(
      <MemoryRouter>
        <CSRView protocol={PROTOCOL} onBack={vi.fn()} />
      </MemoryRouter>,
    )
    expect(container.querySelector('main')).toBeNull()
    expect(await screen.findByRole('group', { name: 'What are you uploading?' })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('radio', { name: /Raw concentration/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
    await waitFor(() => expect(h.api.createStudyReport).toHaveBeenCalledWith('p1', 'nca'))
    expect(await screen.findByRole('heading', { name: '1 · Get the template' })).toBeInTheDocument()
    expect(screen.getByRole('listitem', { current: 'step' })).toHaveTextContent('1. Upload data')
  })

  it('resumes at the results step and generates the narrative', async () => {
    h.api.listStudyReports.mockResolvedValue([{ id: 'sr1' }])
    h.api.getStudyReport.mockResolvedValue(report({ results: RESULTS }))
    h.api.getStudyReportQC.mockResolvedValue({ submission_ready: true, counts: {}, total_checks: 0, findings: [] })
    h.api.generateStudyReport.mockResolvedValue(
      report({ results: RESULTS, generation_status: 'complete', sections: { synopsis: '<p>Mock synopsis</p>' } }),
    )
    render(
      <MemoryRouter>
        <CSRView protocol={PROTOCOL} onBack={vi.fn()} />
      </MemoryRouter>,
    )
    fireEvent.click(await screen.findByRole('button', { name: 'Generate Clinical Study Report' }))
    await waitFor(() => expect(h.api.generateStudyReport).toHaveBeenCalledWith('sr1'))
    expect(await screen.findByText('Mock synopsis')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Export CSR (.docx)' })).toBeEnabled()
    expect(screen.getByRole('button', { name: 'Export CSR (.pdf)' })).toBeEnabled()
  })
})

describe('CSRDatasetUpload', () => {
  it('uploads the chosen file with the LLOQ for raw concentration data', async () => {
    const onAnalyzed = vi.fn()
    const analysed = report({ results: RESULTS })
    h.api.uploadStudyReportDataset.mockResolvedValue(analysed)
    const { container } = render(<CSRDatasetUpload studyReport={report({ input_kind: 'nca' })} onAnalyzed={onAnalyzed} />)

    const analyze = screen.getByRole('button', { name: /Upload & analyze/ })
    expect(analyze).toBeDisabled()

    const file = new File(['id,conc\n1,2'], 'data.csv', { type: 'text/csv' })
    const input = container.querySelector('input[type="file"]') as HTMLInputElement
    fireEvent.change(input, { target: { files: [file] } })
    expect(screen.getByText('data.csv')).toBeInTheDocument()

    fireEvent.change(screen.getByLabelText(/LLOQ/), { target: { value: '5' } })
    expect(analyze).toBeEnabled()
    fireEvent.click(analyze)
    await waitFor(() => expect(h.api.uploadStudyReportDataset).toHaveBeenCalledWith('sr1', file, 5))
    await waitFor(() => expect(onAnalyzed).toHaveBeenCalledWith(analysed))
  })

  it('blocks an invalid LLOQ and shows per-row validation errors from the server', async () => {
    h.api.uploadStudyReportDataset.mockRejectedValue(new h.DatasetValidationError('Dataset has problems', ['Row 3: missing subject', 'Row 9: bad period']))
    const { container } = render(<CSRDatasetUpload studyReport={report({ input_kind: 'nca' })} onAnalyzed={vi.fn()} />)
    const input = container.querySelector('input[type="file"]') as HTMLInputElement
    fireEvent.change(input, { target: { files: [new File(['x'], 'bad.csv')] } })

    fireEvent.change(screen.getByLabelText(/LLOQ/), { target: { value: '-1' } })
    expect(screen.getByText('LLOQ must be a positive number.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Upload & analyze/ })).toBeDisabled()

    fireEvent.change(screen.getByLabelText(/LLOQ/), { target: { value: '' } })
    fireEvent.click(screen.getByRole('button', { name: /Upload & analyze/ }))
    const alert = await screen.findByText('Dataset has problems')
    const box = alert.closest('[role="alert"]') as HTMLElement
    expect(within(box).getByText('Row 3: missing subject')).toBeInTheDocument()
    expect(within(box).getByText('Row 9: bad period')).toBeInTheDocument()
  })
})

describe('CSRResultsReview', () => {
  it('shows the verdict, the pivotal table and the disposition', () => {
    render(<CSRResultsReview results={RESULTS} />)
    expect(screen.getByText(/Overall bioequivalence: Bioequivalent/)).toBeInTheDocument()
    const table = screen.getByRole('table')
    expect(within(table).getByRole('rowheader', { name: 'Cmax' })).toBeInTheDocument()
    expect(within(table).getByText('98.31')).toBeInTheDocument()
    expect(within(table).getByText('Yes')).toBeInTheDocument()
    expect(within(table).getByText('No')).toBeInTheDocument()
    expect(screen.getByText('Subjects')).toBeInTheDocument()
  })
})
