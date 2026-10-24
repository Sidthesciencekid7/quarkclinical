import { useCallback, useSyncExternalStore } from 'react'

function mql(query: string): MediaQueryList | null {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return null
  try {
    return window.matchMedia(query)
  } catch {
    return null
  }
}

/** Live `matchMedia` flag. `fallback` is used where matchMedia does not exist (tests, SSR). */
export function useMediaQuery(query: string, fallback = true): boolean {
  const subscribe = useCallback(
    (onChange: () => void) => {
      const list = mql(query)
      if (!list) return () => {}
      // `resize` is a belt-and-braces second trigger: some environments (device
      // emulation, embedded webviews) change the viewport without delivering a
      // MediaQueryList `change` event. getSnapshot re-reads the real answer.
      let timer: ReturnType<typeof setTimeout> | null = null
      const onResize = () => {
        onChange()
        // The query result can lag the resize event by a frame; look again.
        if (timer) clearTimeout(timer)
        timer = setTimeout(onChange, 120)
      }
      window.addEventListener('resize', onResize)
      const stopResize = () => {
        window.removeEventListener('resize', onResize)
        if (timer) clearTimeout(timer)
      }
      if (typeof list.addEventListener === 'function') {
        list.addEventListener('change', onChange)
        return () => {
          stopResize()
          list.removeEventListener('change', onChange)
        }
      }
      // Safari < 14
      list.addListener(onChange)
      return () => {
        stopResize()
        list.removeListener(onChange)
      }
    },
    [query],
  )
  const getSnapshot = useCallback(() => mql(query)?.matches ?? fallback, [query, fallback])
  return useSyncExternalStore(subscribe, getSnapshot, () => fallback)
}

/** Tailwind `md` (768px) and `lg` (1024px) breakpoints, as the shell uses them. */
export const MD_QUERY = '(min-width: 768px)'
export const LG_QUERY = '(min-width: 1024px)'
