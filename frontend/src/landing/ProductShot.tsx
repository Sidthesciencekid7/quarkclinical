/**
 * A real product screenshot in a dark, device-like window. The wrapper carries
 * `data-lp="clip"`: the engine wipes it in with a clip-path reveal, and
 * `data-lp="rise"` on the outer element grows it into place as it nears the
 * middle of the screen. Without the engine it is just a framed image.
 */
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
    <div data-lp="rise">
      <figure data-lp="clip" className="lp-device m-0">
        <div className="lp-device__bar">
          <span aria-hidden className="lp-device__dot" />
          <span aria-hidden className="lp-device__dot" />
          <span aria-hidden className="lp-device__dot" />
          <span className="ml-3 truncate text-[11px] font-medium text-fg-subtle">{label}</span>
        </div>
        <img
          src={src}
          alt={alt}
          width={width}
          height={height}
          loading="lazy"
          decoding="async"
          className="block h-auto w-full"
        />
      </figure>
    </div>
  )
}
