import type { MutableRefObject, Ref, RefCallback } from 'react'

export type ClassValue = string | number | false | null | undefined

/** Join truthy class names. Caller-supplied classes should be passed last. */
export function cx(...values: ClassValue[]): string {
  let out = ''
  for (const v of values) {
    if (v === false || v === null || v === undefined || v === 0 || v === '') continue
    out += out ? ` ${v}` : String(v)
  }
  return out
}

/** Assign a value to any kind of React ref. */
export function setRef<T>(ref: Ref<T> | undefined, value: T | null): void {
  if (!ref) return
  if (typeof ref === 'function') ref(value)
  else (ref as MutableRefObject<T | null>).current = value
}

/** Merge several refs into one callback ref. */
export function mergeRefs<T>(...refs: Array<Ref<T> | undefined>): RefCallback<T> {
  return (node: T | null) => {
    for (const r of refs) setRef(r, node)
  }
}

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"]), [contenteditable="true"]'

/** Focusable, visible descendants of a container, in DOM order. */
export function getFocusable(container: HTMLElement | null): HTMLElement[] {
  if (!container) return []
  return Array.from(container.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
    el => !el.hasAttribute('hidden') && el.getAttribute('aria-hidden') !== 'true' && el.tabIndex !== -1,
  )
}
