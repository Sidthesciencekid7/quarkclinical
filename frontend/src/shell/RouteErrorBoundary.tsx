import { Component } from 'react'
import type { ErrorInfo, ReactNode } from 'react'
import { Banner } from '../ui'

interface Props {
  children: ReactNode
  /** The boundary clears itself when this changes (e.g. the pathname). */
  resetKey?: string
  /** Replaces the default message. */
  fallback?: (error: Error) => ReactNode
}

interface State {
  error: Error | null
}

/**
 * Keeps a crashing page from blanking the whole shell: the sidebar and top bar
 * stay usable and the page area shows what went wrong.
 */
export class RouteErrorBoundary extends Component<Props, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('Page crashed:', error, info.componentStack)
  }

  componentDidUpdate(prev: Props) {
    if (this.state.error && prev.resetKey !== this.props.resetKey) this.setState({ error: null })
  }

  render() {
    const { error } = this.state
    if (!error) return this.props.children
    if (this.props.fallback) return this.props.fallback(error)
    return (
      <div className="mx-auto max-w-2xl px-4 py-10">
        <Banner tone="danger" title="This page could not be displayed">
          Something went wrong while loading it. Use the sidebar to go elsewhere, or reload the page.
          <span className="mt-1 block text-fg-subtle">{error.message}</span>
        </Banner>
      </div>
    )
  }
}
