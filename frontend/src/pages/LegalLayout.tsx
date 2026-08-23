import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'

export default function LegalLayout({
  title,
  updated,
  children,
}: {
  title: string
  updated: string
  children: ReactNode
}) {
  return (
    <div className="bg-slate-50 min-h-screen">
      <header className="border-b border-slate-200 bg-white">
        <div className="max-w-3xl mx-auto px-5 h-16 flex items-center justify-between">
          <Link to="/" className="flex items-center gap-2.5">
            <span className="w-8 h-8 rounded-lg bg-cyan-50 border border-cyan-200 flex items-center justify-center text-cyan-600 text-sm font-extrabold">
              Q
            </span>
            <span className="font-extrabold tracking-tight text-slate-900">
              Quark <span className="text-cyan-600">AI</span>
            </span>
          </Link>
          <Link to="/" className="text-sm font-medium text-slate-500 hover:text-slate-900 transition-colors">
            Back to home
          </Link>
        </div>
      </header>

      <main className="max-w-3xl mx-auto px-5 py-16">
        <h1 className="text-3xl font-extrabold text-slate-900 tracking-tight mb-2">{title}</h1>
        <p className="text-sm text-slate-400 mb-10">Last updated {updated}</p>
        <div className="prose prose-slate prose-headings:font-bold prose-a:text-cyan-600 max-w-none">
          {children}
        </div>
      </main>
    </div>
  )
}
