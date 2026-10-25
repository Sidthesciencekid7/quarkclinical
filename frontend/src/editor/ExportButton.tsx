import { useEffect, useMemo, useState } from 'react'
import { api, HttpError } from '../api/client'
import type { Drug, GeneratedProtocol } from '../types'
import { Badge, Banner, Button, ChevronDown, Download, FileText, Menu, Modal } from '../ui'
import type { MenuEntry } from '../ui'

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

/**
 * Export pill (primary) opening a Menu of every downloadable document. The
 * restricted randomisation list still needs an explicit confirmation before it
 * downloads, now in a dialog rather than inline in the menu.
 */
export default function ExportButton({ protocol, drug }: Props) {
  const [downloading, setDownloading] = useState<string | null>(null)
  const [confirmSpec, setConfirmSpec] = useState<DownloadSpec | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!error) return
    const t = setTimeout(() => setError(null), 6000)
    return () => clearTimeout(t)
  }, [error])

  const handleDownload = async (spec: DownloadSpec) => {
    setConfirmSpec(null)
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

  /** Choosing a restricted document opens a confirmation first. */
  const handleSelect = (spec: DownloadSpec) => {
    if (spec.restricted) {
      setConfirmSpec(spec)
      return
    }
    void handleDownload(spec)
  }

  const busy = !!downloading
  const busySpec = ALL_DOWNLOADS.find(d => d.key === downloading)

  const entries = useMemo<MenuEntry[]>(() => {
    const item = (spec: DownloadSpec): MenuEntry => ({
      id: spec.key,
      label: spec.label,
      icon: <FileText aria-hidden />,
      trailing: spec.restricted ? <Badge variant="danger">Restricted</Badge> : undefined,
      onSelect: () => handleSelect(spec),
    })
    return [
      { type: 'label', id: 'protocol-label', label: 'Protocol document' },
      ...PROTOCOL_DOWNLOADS.map(item),
      { type: 'separator', id: 'package-sep' },
      { type: 'label', id: 'package-label', label: 'Study package' },
      ...PACKAGE_DOWNLOADS.map(item),
    ]
    // handleSelect closes over state setters and `protocol`/`drug` only.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [protocol.id, protocol.version, drug.inn])

  return (
    <div className="relative">
      <Menu
        label="Export documents"
        placement="bottom-end"
        minWidth={300}
        items={entries}
        trigger={
          <Button
            variant="primary"
            size="md"
            loading={busy}
            leftIcon={<Download aria-hidden />}
            rightIcon={busy ? undefined : <ChevronDown aria-hidden />}
          >
            {busySpec ? `Exporting ${busySpec.format.toUpperCase()}…` : 'Export'}
          </Button>
        }
      />

      {error ? (
        <div className="absolute right-0 top-full z-30 mt-2 w-[22rem] max-w-[calc(100vw-2rem)]">
          <Banner tone="danger" title="Export failed" onDismiss={() => setError(null)}>
            {error}
          </Banner>
        </div>
      ) : null}

      <Modal
        open={confirmSpec !== null}
        onClose={() => setConfirmSpec(null)}
        title="Download the restricted randomisation list?"
        size="sm"
        footer={
          <>
            <Button variant="secondary" onClick={() => setConfirmSpec(null)}>
              Cancel
            </Button>
            <Button variant="danger" solid onClick={() => confirmSpec && void handleDownload(confirmSpec)}>
              Download anyway
            </Button>
          </>
        }
      >
        <p className="text-body text-fg-muted">
          This file contains the <span className="font-semibold text-fg">unblinded treatment assignment for every subject</span>.
          File it in the restricted section of the TMF, never in the protocol document or anywhere the study team can reach it.
        </p>
      </Modal>
    </div>
  )
}

/** A missing route means the backend build simply doesn't render that document
 *  yet: say so, rather than showing the reader a bare "Not Found". */
function errorMessage(e: unknown, spec: DownloadSpec): string {
  if (e instanceof HttpError && e.status === 404) {
    return `${spec.label} isn't available on the connected backend yet.`
  }
  if (e instanceof Error && e.message) return e.message
  return spec.format === 'pdf'
    ? 'PDF export failed. Ensure reportlab is installed.'
    : 'Export failed. Please try again.'
}
