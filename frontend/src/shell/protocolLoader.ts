import { api } from '../api/client'
import type { Drug, GeneratedProtocol, ProtocolSummary } from '../types'
import { capitalize, STUDY_TYPE_LABELS } from './format'

export interface ProtocolBundle {
  protocol: GeneratedProtocol
  /** What ProtocolEditor needs to title and scope itself. */
  drug: Drug
  /** The list-row shape CSRView takes. */
  summary: ProtocolSummary
  /** "Adalimumab · Biosimilar PK" (breadcrumbs, page titles). */
  label: string
}

type WithDrug = GeneratedProtocol & { drug_inn?: string | null; drug_brand_names?: string[]; guideline_outdated?: boolean }

/**
 * Load a protocol by id together with its drug's name. GET /api/protocols/{id}
 * carries `drug_inn` / `drug_brand_names`; if an older backend leaves them out,
 * the org's protocol list (which always has them) is used to fill the gap.
 */
export async function loadProtocolBundle(id: string): Promise<ProtocolBundle> {
  const protocol = (await api.getProtocol(id)) as WithDrug
  let inn = protocol.drug_inn ?? null
  let brands = protocol.drug_brand_names ?? []
  if (!inn) {
    try {
      const hit = (await api.listProtocols()).find(p => p.id === id)
      inn = hit?.drug_inn ?? null
      brands = hit?.drug_brand_names ?? brands
    } catch {
      /* the editor can still open without the drug's display name */
    }
  }
  const drugInn = inn ?? 'unknown drug'
  const drug: Drug = {
    id: protocol.drug_id,
    inn: drugInn,
    brand_names: brands,
    rxnorm_id: null,
    updated_at: protocol.created_at,
  }
  const summary: ProtocolSummary = {
    id: protocol.id,
    drug_id: protocol.drug_id,
    drug_inn: drugInn,
    drug_brand_names: brands,
    study_type: protocol.study_type,
    guideline_version: protocol.guideline_version,
    guideline_outdated: protocol.guideline_outdated ?? false,
    version: protocol.version,
    status: protocol.status,
    section_count: Object.values(protocol.sections ?? {}).filter(Boolean).length,
    created_at: protocol.created_at,
  }
  return {
    protocol,
    drug,
    summary,
    label: `${capitalize(drugInn)} · ${STUDY_TYPE_LABELS[protocol.study_type] ?? protocol.study_type}`,
  }
}
