/**
 * Study-type tables shared by the chat cards (moved out of the retired
 * StudyTypeSelector wizard step).
 *
 * `STUDY_TYPES_BY_MODALITY` mirrors services/design_decisions.py on the
 * backend, which is the authoritative, enforced source of truth (422 on a
 * mismatch). This copy only decides what the UI offers; the study_type card
 * itself is built by the server from the same rule.
 */
import type { DrugModality, StudyType } from '../types'

export const STUDY_TYPES: readonly StudyType[] = ['BE', 'biosimilar_pk', 'food_effect']

export const STUDY_TYPES_BY_MODALITY: Record<'biologic' | 'small_molecule', StudyType[]> = {
  biologic: ['biosimilar_pk'],
  small_molecule: ['BE', 'food_effect'],
}

/** The study types a modality may run, or null when it is unknown (do not restrict). */
export function allowedStudyTypes(modality: DrugModality | string | undefined | null): StudyType[] | null {
  if (modality === 'biologic' || modality === 'small_molecule') {
    return STUDY_TYPES_BY_MODALITY[modality]
  }
  return null
}

export function isStudyType(value: unknown): value is StudyType {
  return value === 'BE' || value === 'biosimilar_pk' || value === 'food_effect'
}

/** Short names (chips, summaries). */
export const STUDY_TYPE_LABELS: Record<StudyType, string> = {
  BE: 'Bioequivalence',
  biosimilar_pk: 'Biosimilar PK',
  food_effect: 'Food Effect',
}

/** Long names (the protocol summary table). */
export const STUDY_TYPE_LONG_LABELS: Record<StudyType, string> = {
  BE: 'Bioequivalence (Small Molecule)',
  biosimilar_pk: 'Biosimilar PK Similarity (Biologic)',
  food_effect: 'Food Effect',
}

/** Label for a study-type string that may not be one of the three known values. */
export function studyTypeLabel(value: string | null | undefined): string {
  if (value && isStudyType(value)) return STUDY_TYPE_LABELS[value]
  return value ?? ''
}

export interface StudyTypeDetail {
  label: string
  subtitle: string
  reg: string
  description: string
  design: string
}

export const STUDY_TYPE_DETAILS: Record<StudyType, StudyTypeDetail> = {
  BE: {
    label: 'Bioequivalence',
    subtitle: 'Small molecule',
    reg: '21 CFR Part 320 · FDA BE Guidance 2014 · ICH E6(R2)',
    description:
      'Generic or new formulations of small-molecule drugs. Shows PK equivalence to the Reference Listed Drug: the 90% CI for Cmax and AUC must sit within 80.00-125.00%.',
    design: '2-period crossover · single dose · fasting or fed',
  },
  biosimilar_pk: {
    label: 'Biosimilar PK Similarity',
    subtitle: 'Biologic',
    reg: 'FDA 351(k) pathway · FDA Biosimilar Guidance 2019 · ICH E6(R2)',
    description:
      'Proteins, antibodies and peptides. Establishes PK similarity between the biosimilar and its reference product, with immunogenicity (ADA and NAb) sampling.',
    design: 'Parallel group · single dose · immunogenicity follow-up',
  },
  food_effect: {
    label: 'Food Effect',
    subtitle: 'Fed vs fasted',
    reg: 'FDA Food-Effect BA/BE Guidance 2002 · 21 CFR 320.25',
    description:
      'Assesses the effect of a high-fat, high-calorie meal on absorption and bioavailability of an oral product.',
    design: '2-period crossover · single dose · fed vs fasted',
  },
}
