import typography from '@tailwindcss/typography'

/**
 * QuarkClinical design tokens (redesign F1).
 *
 * The *values* that change between the light app and the dark landing live as
 * CSS variables in src/index.css (`:root` + `[data-surface="dark"]`); this file
 * mirrors the static scales and maps the surface-aware ones (`fg`, `surface`,
 * `line`) onto those variables. Because the surface tokens are plain CSS
 * variables, Tailwind opacity modifiers (e.g. `text-fg/50`) do not apply to
 * them - use the explicit `fg-muted` / `fg-subtle` steps instead.
 *
 * NOTE: Tailwind's built-in `cyan` and `slate` palettes are left untouched so
 * un-migrated pages keep rendering. New code uses `ink`, `navy`, `accent`.
 */

/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  theme: {
    extend: {
      fontFamily: {
        sans: ['Figtree', 'Inter', 'system-ui', 'sans-serif'],
      },

      colors: {
        // Brand navy (landing, dark surfaces). 900 === the legacy --navy.
        navy: {
          950: '#081226',
          900: '#0B1E3D',
          800: '#122548',
          700: '#1A3260',
        },
        // Cyan accent. On white use 700 for small text (5.4:1); 600 is for
        // icons and large text only.
        accent: {
          50: '#ECFEFF',
          100: '#CFFAFE',
          200: '#A5F3FC',
          300: '#67E8F9',
          400: '#22D3EE',
          500: '#06B6D4',
          600: '#0891B2',
          700: '#0E7490',
        },
        // Neutrals (zinc scale). 400 is placeholder/disabled only.
        ink: {
          50: '#FAFAFA',
          100: '#F4F4F5',
          200: '#E4E4E7',
          300: '#D4D4D8',
          400: '#A1A1AA',
          500: '#71717A',
          600: '#52525B',
          700: '#3F3F46',
          800: '#27272A',
          900: '#18181B',
          950: '#0A0A0A',
        },

        // Surface-aware tokens (resolved by the CSS variables in index.css).
        fg: {
          DEFAULT: 'var(--fg)',
          muted: 'var(--fg-muted)',
          subtle: 'var(--fg-subtle)',
        },
        surface: {
          DEFAULT: 'var(--bg)',
          muted: 'var(--bg-muted)',
          raised: 'var(--bg-raised)',
          pop: 'var(--bg-pop)',
          hover: 'var(--hover)',
          canvas: 'var(--canvas)',
        },
        overlay: 'var(--overlay)',
        line: {
          DEFAULT: 'var(--border)',
          strong: 'var(--border-strong)',
        },
      },

      borderRadius: {
        pill: '9999px',
        card: '16px',
        tile: '20px',
        input: '12px',
        composer: '24px',
      },

      // Light values are in index.css (--shadow-*); dark surfaces override them.
      //   xs       0 1px 2px rgb(10 10 10 / .04)
      //   card     0 1px 2px rgb(10 10 10 / .04), 0 1px 3px rgb(10 10 10 / .03)
      //   pop      0 8px 24px -6px rgb(10 10 10 / .14)
      //   composer 0 8px 32px -8px rgb(10 10 10 / .16), 0 2px 6px rgb(10 10 10 / .06)
      boxShadow: {
        xs: 'var(--shadow-xs)',
        card: 'var(--shadow-card)',
        pop: 'var(--shadow-pop)',
        composer: 'var(--shadow-composer)',
      },

      // Focus ring: 2px ring with a 2px offset, both surface-aware.
      ringColor: {
        DEFAULT: 'var(--ring)',
      },
      ringOffsetColor: {
        DEFAULT: 'var(--ring-offset)',
      },

      // Type scale. Usage: `text-h1`, `text-body`, ... (Figtree throughout;
      // prefer semibold over extrabold. Use `tabular-nums` for numbers.)
      fontSize: {
        display: ['3.5rem', { lineHeight: '3.75rem', letterSpacing: '-0.03em', fontWeight: '600' }],
        h1: ['2rem', { lineHeight: '2.375rem', letterSpacing: '-0.02em', fontWeight: '600' }],
        h2: ['1.375rem', { lineHeight: '1.75rem', letterSpacing: '-0.01em', fontWeight: '600' }],
        h3: ['1.0625rem', { lineHeight: '1.5rem', fontWeight: '600' }],
        body: ['0.9375rem', { lineHeight: '1.5rem' }],
        small: ['0.8125rem', { lineHeight: '1.25rem' }],
        eyebrow: ['0.6875rem', { lineHeight: '1rem', letterSpacing: '0.08em', fontWeight: '600' }],
      },

      transitionDuration: {
        fast: '150ms',
        panel: '200ms',
      },
      transitionTimingFunction: {
        'out-soft': 'cubic-bezier(0.16, 1, 0.3, 1)',
      },

      keyframes: {
        'ui-pop-in': {
          from: { opacity: '0', transform: 'translateY(4px) scale(0.98)' },
          to: { opacity: '1', transform: 'translateY(0) scale(1)' },
        },
        'ui-fade-in': {
          from: { opacity: '0' },
          to: { opacity: '1' },
        },
      },
      animation: {
        'ui-pop-in': 'ui-pop-in 150ms ease-out both',
        'ui-fade-in': 'ui-fade-in 200ms ease-out both',
      },
    },
  },
  plugins: [typography],
}
