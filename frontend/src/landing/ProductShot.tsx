/** A real product screenshot in a light, minimal browser frame. */
export function ProductShot({
  src,
  alt,
  label,
  width,
  height,
}: {
  src: string
  alt: string
  label: string
  width: number
  height: number
}) {
  return (
    <figure className="m-0 overflow-hidden rounded-card border border-line bg-surface shadow-pop">
      <div className="flex h-9 items-center gap-1.5 border-b border-line bg-surface-muted px-3.5">
        <span aria-hidden className="h-2.5 w-2.5 rounded-full bg-ink-300" />
        <span aria-hidden className="h-2.5 w-2.5 rounded-full bg-ink-300" />
        <span aria-hidden className="h-2.5 w-2.5 rounded-full bg-ink-300" />
        <span className="ml-3 truncate text-[11px] font-medium text-fg-muted">{label}</span>
      </div>
      <img src={src} alt={alt} width={width} height={height} loading="lazy" decoding="async" className="block h-auto w-full" />
    </figure>
  )
}
