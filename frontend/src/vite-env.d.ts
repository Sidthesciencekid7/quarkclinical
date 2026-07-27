/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_CLERK_PUBLISHABLE_KEY?: string
  readonly VITE_API_BASE_URL?: string
  readonly VITE_SENTRY_DSN?: string
  readonly VITE_SENTRY_ENV?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}

/** Clerk attaches a global instance to window when ClerkProvider is mounted.
 *  Used by the API client to fetch a fresh session token outside React. */
interface Window {
  Clerk?: {
    session?: {
      getToken: () => Promise<string | null>
    }
  }
}
