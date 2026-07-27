import React from 'react'
import ReactDOM from 'react-dom/client'
import * as Sentry from '@sentry/react'
import { ClerkProvider } from '@clerk/clerk-react'
import App from './App'
import { CLERK_KEY, authEnabled } from './lib/auth'
import './index.css'

// Error monitoring — a no-op unless VITE_SENTRY_DSN is set at build time, so
// this is safe to ship before the Sentry project exists. Set the var in Vercel
// and redeploy to activate. Publishable-safe: a DSN is not a secret.
const SENTRY_DSN = import.meta.env.VITE_SENTRY_DSN as string | undefined
if (SENTRY_DSN) {
  Sentry.init({
    dsn: SENTRY_DSN,
    environment: (import.meta.env.VITE_SENTRY_ENV as string | undefined) ?? 'production',
    // Keep sampling low to stay within the free tier; raise if you need more traces.
    tracesSampleRate: 0.1,
    sendDefaultPii: false,
  })
}

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    {authEnabled ? (
      <ClerkProvider publishableKey={CLERK_KEY!} afterSignOutUrl="/">
        <App />
      </ClerkProvider>
    ) : (
      <App />
    )}
  </React.StrictMode>,
)
