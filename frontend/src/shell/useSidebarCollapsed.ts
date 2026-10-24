import { useCallback, useState } from 'react'

/** localStorage key for the user's sidebar preference ('1' = icon rail). */
export const SIDEBAR_COLLAPSED_KEY = 'qc_sidebar_collapsed'

export function readSidebarCollapsed(): boolean {
  try {
    return window.localStorage.getItem(SIDEBAR_COLLAPSED_KEY) === '1'
  } catch {
    return false
  }
}

function writeSidebarCollapsed(value: boolean): void {
  try {
    window.localStorage.setItem(SIDEBAR_COLLAPSED_KEY, value ? '1' : '0')
  } catch {
    /* storage blocked (private window, quota): the preference just will not persist */
  }
}

/** Sidebar collapsed preference, remembered across visits. Never throws. */
export function useSidebarCollapsed(): [boolean, (next: boolean | ((prev: boolean) => boolean)) => void] {
  const [collapsed, setCollapsedState] = useState<boolean>(readSidebarCollapsed)
  const setCollapsed = useCallback((next: boolean | ((prev: boolean) => boolean)) => {
    setCollapsedState(prev => {
      const value = typeof next === 'function' ? next(prev) : next
      writeSidebarCollapsed(value)
      return value
    })
  }, [])
  return [collapsed, setCollapsed]
}
