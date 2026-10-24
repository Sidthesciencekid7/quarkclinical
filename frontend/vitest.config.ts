import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

// Unit tests for the design system (src/ui) and other pure-UI code.
// Separate from vite.config.ts so the dev-server proxy is not loaded in tests.
export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    include: ['src/**/*.{test,spec}.{ts,tsx}'],
    css: false,
    restoreMocks: true,
  },
})
