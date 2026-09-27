# Deploying QuarkClinical (Vercel + Railway + Clerk)

This gets the app live: **backend + Postgres on Railway**, **frontend on Vercel**,
**auth on Clerk (production instance)**. The code is already deployment-ready
(env-driven CORS + API base, Dockerfile, `vercel.json`); the steps below are the
account/config work only you can do (they need your logins, secrets, and billing).

> **Repo layout note.** This repo's app lives in `protocol app/backend` and
> `protocol app/frontend` (the folder name has a space — set each host's *root
> directory* to that path exactly; if a tool chokes on the space, rename the
> folder to `protocol-app` first). The React-Native files at the repo root are a
> separate old app and are not deployed.

---

## 0. Prerequisites (create these accounts)

- **Railway** account (railway.app) — hosts the FastAPI backend + Postgres.
- **Vercel** account (vercel.com) — hosts the React frontend.
- **Clerk** account (clerk.com) — you already have a *dev* instance; you'll add a
  *production* instance.
- **Anthropic API credits** — console.anthropic.com → Plans & Billing. Generation
  spends real credits; a Claude Pro subscription does **not** include them.
- Push this repo to GitHub (Railway and Vercel both deploy from a Git repo).

---

## 1. Backend + database on Railway

1. **New Project → Deploy from GitHub repo**, pick this repo.
2. In the service settings, set **Root Directory** to `protocol app/backend`.
   Railway will detect the `Dockerfile` and build it. (If you prefer Nixpacks,
   delete the Dockerfile and it'll use the `Procfile` instead.)
3. **Add a Postgres plugin** to the project (New → Database → PostgreSQL). Railway
   auto-creates a `DATABASE_URL` variable — but it's `postgresql://...`, and this
   app uses the async driver, so add a variable that rewrites it:
   - `DATABASE_URL = ${{Postgres.DATABASE_URL}}` then edit it to start with
     `postgresql+asyncpg://` (replace the `postgresql://` scheme). *Simplest:*
     copy the Postgres connection string and set
     `DATABASE_URL=postgresql+asyncpg://<user>:<pass>@<host>:<port>/<db>`.
4. Set the backend **environment variables** (service → Variables):
   | Variable | Value |
   |---|---|
   | `DATABASE_URL` | `postgresql+asyncpg://…` (from step 3) |
   | `ANTHROPIC_API_KEY` | your Anthropic key |
   | `CLERK_ISSUER` | your **production** Clerk issuer (step 3 below) — e.g. `https://clerk.yourdomain.com` |
   | `CLERK_SECRET_KEY` | your **production** Clerk secret (`sk_live_…`) |
   | `ALLOWED_ORIGINS` | *(only if NOT using the Vercel proxy)* your frontend URL, e.g. `https://your-app.vercel.app` |
5. Deploy. Grab the backend's public URL (e.g. `https://protocolai-backend.up.railway.app`).
6. **One-time database setup** — open the Railway service **Shell** (or `railway run`) and run:
   ```bash
   python create_tables.py          # creates all tables (idempotent)
   python seed_users.py --email you@yourdomain.com --role generator
   ```
   `seed_users.py` also provisions the demo `@pkbe.local` users; pass your own
   Clerk-login email so email-binding (see below) lets you in.

> **R / PowerTOST:** not installed in the container — the sample-size engine
> automatically falls back to the validated Python implementation, so nothing
> breaks. Add R only if you need the exact R-PowerTOST provenance line.

---

## 2. Frontend on Vercel

1. **Add New → Project**, import this repo.
2. Set **Root Directory** to `protocol app/frontend`. Framework preset: **Vite**.
3. Edit **`vercel.json`** (already in the frontend folder): replace
   `REPLACE-WITH-YOUR-BACKEND.up.railway.app` with your Railway backend host from
   step 1.5. This proxies `/api/*` from the Vercel domain to Railway **server-side**,
   so the browser only ever calls same-origin `/api` — no CORS, and it avoids the
   cross-origin `Authorization`-header-stripping issue.
4. Set the frontend **environment variable**:
   | Variable | Value |
   |---|---|
   | `VITE_CLERK_PUBLISHABLE_KEY` | your **production** Clerk publishable key (`pk_live_…`) |

   Leave `VITE_API_BASE_URL` **unset** (the Vercel proxy handles routing). Only set
   it if you deliberately call the backend cross-origin instead of via the proxy —
   and then also set `ALLOWED_ORIGINS` on the backend.
5. Deploy. Note the frontend URL (e.g. `https://your-app.vercel.app`).

---

## 3. Clerk production instance

Your current Clerk instance (`stirred-kiwi-4.clerk.accounts.dev`) is a **dev**
instance — fine for testing, not for real users.

1. In the Clerk dashboard, **create a Production instance** for the app.
2. Set its allowed domain to your Vercel URL (or your custom domain).
3. Copy the production **publishable key** (`pk_live_…`) → Vercel
   `VITE_CLERK_PUBLISHABLE_KEY` (step 2.4).
4. Copy the production **secret key** (`sk_live_…`) → Railway `CLERK_SECRET_KEY`.
5. Copy the production **issuer / Frontend API URL** → Railway `CLERK_ISSUER`.
6. Redeploy both services so the new keys take effect.

> With `CLERK_ISSUER` set, the backend enforces auth and binds identity to the
> verified Clerk email (a user's Clerk-login email must exist in the users table —
> that's what `seed_users.py --email` is for). The "Acting as" picker only appears
> when Clerk is off (local dev).

---

## 4. Post-deploy checks

- `GET https://<backend>/health` → `{"status":"ok"}` (DB + Anthropic reachable).
- Open the Vercel URL → sign in with a Clerk account whose email you provisioned →
  **My Reports** loads → **New Protocol** generates (spends Anthropic credits) →
  the new protocol appears in **My Reports** and re-opens in the editor.
- If generation 402s: add Anthropic credits. If every API call 401s: the Clerk
  prod keys/issuer don't match between Vercel and Railway.

---

## 5. Later / optional

- **Custom domain**: add it in Vercel, update the Clerk production domain, and add
  it to `ALLOWED_ORIGINS` if you use direct cross-origin instead of the proxy.
- **Stripe** (billing): not wired yet — the pricing page is presentational.
- **Migrations**: `create_tables.py` (create-all) is fine for launch; move to
  `alembic upgrade head` in the start command once you're iterating on schema.
- **Secrets hygiene**: never commit real keys — `.env` is gitignored; set all
  secrets in the Railway/Vercel dashboards.
