import { Fragment } from 'react'

interface StepperProps {
  steps: string[]
  currentStep: number
}

function CheckIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 15 15" fill="none">
      <path d="M2.5 7.5l3.5 3.5 6.5-7" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}

export default function Stepper({ steps, currentStep }: StepperProps) {
  return (
    <div className="flex items-start">
      {steps.map((label, idx) => {
        const done = idx < currentStep
        const active = idx === currentStep

        return (
          <Fragment key={label}>
            <div className="flex flex-col items-center" style={{ minWidth: 64 }}>
              {/* Circle */}
              <div
                className={[
                  'w-10 h-10 rounded-full flex items-center justify-center text-sm font-bold transition-all duration-300',
                  done
                    ? 'bg-emerald-500 text-white shadow-sm'
                    : active
                    ? 'bg-cyan-600 text-white glow-pulse shadow-sm'
                    : 'bg-white text-slate-400 border-2 border-slate-200',
                ].join(' ')}
              >
                {done ? <CheckIcon /> : <span>{idx + 1}</span>}
              </div>

              {/* Label */}
              <span
                className={[
                  'mt-2.5 text-[11px] font-semibold text-center leading-tight tracking-wide uppercase',
                  done ? 'text-emerald-600' : active ? 'text-cyan-700' : 'text-slate-400',
                ].join(' ')}
                style={{ maxWidth: 72 }}
              >
                {label}
              </span>
            </div>

            {/* Connecting track */}
            {idx < steps.length - 1 && (
              <div className="flex-1 mt-5 mx-1">
                <div className="h-0.5 w-full bg-slate-200 rounded-full overflow-hidden">
                  <div
                    className={[
                      'h-full rounded-full transition-all duration-500 ease-out',
                      done ? 'bg-emerald-400 w-full' : 'bg-transparent w-0',
                    ].join(' ')}
                  />
                </div>
              </div>
            )}
          </Fragment>
        )
      })}
    </div>
  )
}
