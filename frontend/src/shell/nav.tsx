/**
 * Sidebar navigation definitions and the "which item is active" rules.
 * Kept free of React state so the rules can be unit-tested directly.
 */
import type { ReactNode } from 'react'
import {
  Activity,
  Calculator,
  ClipboardList,
  FileText,
  House,
  Pill,
  Receipt,
  Sparkles,
  Syringe,
  Users,
} from '../ui'
import { CHAT_ENABLED, NEW_CHAT_PATH, newStudyHref } from './config'
import type { StudyKind, StudyPreset } from './config'

export interface NavItemDef {
  id: string
  label: string
  to: string
  icon: ReactNode
  /** Shows the "New" pill. */
  isNew?: boolean
}

export interface PinnedItemDef {
  id: string
  label: string
  kind: StudyKind
  preset: StudyPreset
  icon: ReactNode
}

export const MAIN_NAV: NavItemDef[] = [
  { id: 'home', label: 'Home', to: '/app', icon: <House /> },
  { id: 'new', label: 'New study', to: NEW_CHAT_PATH, icon: <Sparkles />, isNew: true },
  { id: 'protocols', label: 'Protocols', to: '/app/protocols', icon: <FileText /> },
  { id: 'budgets', label: 'Budgets', to: '/app/budgets', icon: <Calculator /> },
  { id: 'reports', label: 'Study reports', to: '/app/reports', icon: <ClipboardList /> },
  { id: 'team', label: 'Team', to: '/app/team', icon: <Users /> },
]

export const PINNED: PinnedItemDef[] = [
  { id: 'pin-biosimilar', label: 'Biosimilar PK protocol', kind: 'protocol', preset: 'biosimilar_pk', icon: <Syringe /> },
  { id: 'pin-be', label: 'Bioequivalence protocol', kind: 'protocol', preset: 'be', icon: <Pill /> },
  { id: 'pin-food', label: 'Food-effect protocol', kind: 'protocol', preset: 'food_effect', icon: <Activity /> },
  { id: 'pin-price', label: 'Price a study', kind: 'budget', preset: 'price', icon: <Receipt /> },
]

function presetOf(search: string): string | null {
  return new URLSearchParams(search).get('preset')
}

/** Is the main-nav item `id` the active one for this location? */
export function isMainNavActive(id: string, pathname: string, search = ''): boolean {
  const path = pathname.replace(/\/+$/, '') || '/'
  switch (id) {
    case 'home':
      return path === '/app'
    case 'new':
      // The chat routes, plus the classic wizards (until chat replaces them)
      // when they were not opened through a pinned preset.
      if (path === NEW_CHAT_PATH || path.startsWith('/app/chat/')) return !presetOf(search)
      if (path === '/app/protocol' || path === '/app/budgeting') return !presetOf(search)
      return false
    case 'protocols':
      return path === '/app/protocols' || path.startsWith('/app/protocols/')
    case 'budgets':
      return path === '/app/budgets' || path.startsWith('/app/budgets/')
    case 'reports':
      return path === '/app/reports'
    case 'team':
      return path === '/app/team'
    default:
      return false
  }
}

/** Is this pinned shortcut the page currently open? Matches on path AND preset. */
export function isPinnedActive(item: PinnedItemDef, pathname: string, search = ''): boolean {
  const path = pathname.replace(/\/+$/, '') || '/'
  if (presetOf(search) !== item.preset) return false
  if (CHAT_ENABLED) return path === NEW_CHAT_PATH && new URLSearchParams(search).get('kind') === item.kind
  return path === (item.kind === 'protocol' ? '/app/protocol' : '/app/budgeting')
}

export function pinnedHref(item: PinnedItemDef): string {
  return newStudyHref(item.kind, item.preset)
}
