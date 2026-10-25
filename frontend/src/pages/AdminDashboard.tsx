import { useEffect, useState } from 'react'
import { useClerk } from '@clerk/clerk-react'
import { api } from '../api/client'
import { authEnabled } from '../lib/auth'
import { Badge, Banner, Button, Card, LogOut, Spinner } from '../ui'
import type { BadgeVariant } from '../ui'
import type { AdminOrgRow, Lead } from '../types'

/** Operator-only dashboard (ADMIN_EMAILS-gated on the backend) — the
 *  "requests made by organisations" view: Contact Sales leads and
 *  self-serve orgs waiting on manual license activation. Reached from the
 *  workspace menu for admins; the backend is the source of truth on
 *  access, so this page just tries the fetch and shows "Access denied" on
 *  a 403 rather than duplicating the ADMIN_EMAILS check client-side.
 *
 *  Clerk's hooks only work under <ClerkProvider>, which is mounted only when
 *  sign-in is enabled. `authEnabled` is a module constant, so choosing the
 *  component here keeps every render on the same branch (no conditional
 *  hooks) and lets the page open in a local sign-in-off run, where there is
 *  nothing to log out of. */
export default function AdminDashboard() {
  return authEnabled ? <AdminDashboardWithClerk /> : <AdminDashboardView onLogout={null} />
}

function AdminDashboardWithClerk() {
  const clerk = useClerk()
  return <AdminDashboardView onLogout={() => { void clerk.signOut({ redirectUrl: '/admin' }) }} />
}

const LICENSE_VARIANT: Record<string, BadgeVariant> = {
  active: 'success',
  pending: 'warning',
}

function AdminDashboardView({ onLogout }: { onLogout: (() => void) | null }) {
  const [leads, setLeads] = useState<Lead[] | null>(null)
  const [orgs, setOrgs] = useState<AdminOrgRow[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [pendingActionId, setPendingActionId] = useState<string | null>(null)

  const load = async () => {
    setLoading(true)
    setError(null)
    try {
      const [leadRows, orgRows] = await Promise.all([api.listLeads(), api.adminListOrgs()])
      setLeads(leadRows)
      setOrgs(orgRows)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not load admin data.')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load() }, [])

  const handleActivate = async (orgId: string) => {
    setPendingActionId(orgId)
    try {
      await api.activateLicense(orgId)
      await load()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not activate license.')
    } finally {
      setPendingActionId(null)
    }
  }

  const handleSuspend = async (orgId: string) => {
    setPendingActionId(orgId)
    try {
      await api.suspendLicense(orgId)
      await load()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not suspend license.')
    } finally {
      setPendingActionId(null)
    }
  }

  const handleDeleteLead = async (leadId: string) => {
    if (!window.confirm('Delete this lead permanently?')) return
    setPendingActionId(leadId)
    try {
      await api.deleteLead(leadId)
      await load()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not delete lead.')
    } finally {
      setPendingActionId(null)
    }
  }

  // /admin sits outside the app shell, so this page owns its <main> landmark.
  if (loading) {
    return (
      <main id="main-content" className="flex min-h-[50vh] items-center justify-center">
        <div role="status" className="flex items-center gap-2 text-body text-fg-muted">
          <Spinner size={18} />
          <span>Loading…</span>
        </div>
      </main>
    )
  }

  if (error) {
    return (
      <main id="main-content" className="mx-auto w-full max-w-md px-4 py-16">
        <Card padding="lg" className="text-center">
          <h1 className="text-h2 text-fg">Access denied</h1>
          <p className="mt-2 text-body text-fg-muted">{error}</p>
          <div className="mt-6 flex flex-wrap items-center justify-center gap-2">
            <Button variant="secondary" onClick={() => { void load() }}>
              Try again
            </Button>
            {onLogout ? (
              <Button variant="ghost" leftIcon={<LogOut aria-hidden />} onClick={onLogout}>
                Log out
              </Button>
            ) : null}
          </div>
        </Card>
      </main>
    )
  }

  return (
    <main id="main-content" className="mx-auto w-full max-w-5xl space-y-8 px-4 py-8 sm:px-8 sm:py-10">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-h1 text-fg">Admin dashboard</h1>
          <p className="mt-1 text-body text-fg-muted">
            Contact-sales requests and organizations waiting on a license.
          </p>
        </div>
        {onLogout ? (
          <Button variant="secondary" leftIcon={<LogOut aria-hidden />} onClick={onLogout}>
            Log out
          </Button>
        ) : null}
      </div>

      {!onLogout ? (
        <Banner tone="info">Sign-in is off in this environment, so admin access is not enforced here.</Banner>
      ) : null}

      <section aria-labelledby="admin-leads">
        <h2 id="admin-leads" className="mb-3 text-h3 text-fg">
          Contact sales leads ({leads?.length ?? 0})
        </h2>
        <Card padding="none" className="overflow-hidden">
          {!leads || leads.length === 0 ? (
            <p className="p-6 text-body text-fg-subtle">No leads yet.</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[760px] text-body">
                <thead className="bg-surface-muted text-left text-eyebrow uppercase text-fg-subtle">
                  <tr>
                    <th className="px-4 py-3 font-semibold">Company</th>
                    <th className="px-4 py-3 font-semibold">Contact</th>
                    <th className="px-4 py-3 font-semibold">Team size</th>
                    <th className="px-4 py-3 font-semibold">Message</th>
                    <th className="px-4 py-3 font-semibold">Status</th>
                    <th className="px-4 py-3 font-semibold">Received</th>
                    <th className="px-4 py-3"><span className="sr-only">Actions</span></th>
                  </tr>
                </thead>
                <tbody>
                  {leads.map(lead => (
                    <tr key={lead.id} className="border-t border-line">
                      <td className="px-4 py-3 font-semibold text-fg">{lead.company_name}</td>
                      <td className="px-4 py-3 text-fg-muted">
                        {lead.contact_name}
                        <div className="text-small text-fg-subtle">{lead.contact_email}</div>
                      </td>
                      <td className="px-4 py-3 tabular-nums text-fg-muted">{lead.team_size_estimate || '—'}</td>
                      <td className="max-w-xs truncate px-4 py-3 text-fg-muted">{lead.message || '—'}</td>
                      <td className="px-4 py-3">
                        <Badge>{lead.status}</Badge>
                      </td>
                      <td className="whitespace-nowrap px-4 py-3 text-small tabular-nums text-fg-subtle">
                        {new Date(lead.created_at).toLocaleString()}
                      </td>
                      <td className="px-4 py-3 text-right">
                        <Button
                          variant="danger"
                          size="sm"
                          onClick={() => handleDeleteLead(lead.id)}
                          disabled={pendingActionId === lead.id}
                        >
                          {pendingActionId === lead.id ? 'Deleting…' : 'Delete'}
                        </Button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      </section>

      <section aria-labelledby="admin-orgs">
        <h2 id="admin-orgs" className="mb-3 text-h3 text-fg">
          Organizations ({orgs?.length ?? 0})
        </h2>
        <Card padding="none" className="overflow-hidden">
          {!orgs || orgs.length === 0 ? (
            <p className="p-6 text-body text-fg-subtle">No organizations yet.</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[760px] text-body">
                <thead className="bg-surface-muted text-left text-eyebrow uppercase text-fg-subtle">
                  <tr>
                    <th className="px-4 py-3 font-semibold">Organization</th>
                    <th className="px-4 py-3 font-semibold">Owner</th>
                    <th className="px-4 py-3 font-semibold">License</th>
                    <th className="px-4 py-3 font-semibold">Days left</th>
                    <th className="px-4 py-3 font-semibold">Seats</th>
                    <th className="px-4 py-3 font-semibold">Created</th>
                    <th className="px-4 py-3"><span className="sr-only">Actions</span></th>
                  </tr>
                </thead>
                <tbody>
                  {orgs.map(org => (
                    <tr key={org.id} className="border-t border-line">
                      <td className="px-4 py-3 font-semibold text-fg">
                        {org.name}
                        <div className="text-small font-normal text-fg-subtle">{org.slug}</div>
                      </td>
                      <td className="px-4 py-3 text-fg-muted">{org.owner_email || '—'}</td>
                      <td className="px-4 py-3">
                        <Badge variant={LICENSE_VARIANT[org.license_status ?? ''] ?? 'neutral'} dot>
                          {org.license_status || 'none'}
                        </Badge>
                      </td>
                      <td className="px-4 py-3 tabular-nums text-fg-muted">
                        {org.days_remaining === null
                          ? '—'
                          : org.days_remaining < 0
                            ? <span className="font-semibold text-[color:var(--bad-fg)]">Expired</span>
                            : `${org.days_remaining}d`}
                      </td>
                      <td className="px-4 py-3 tabular-nums text-fg-muted">{org.seats_used}/{org.seat_count ?? '—'}</td>
                      <td className="whitespace-nowrap px-4 py-3 text-small tabular-nums text-fg-subtle">
                        {new Date(org.created_at).toLocaleString()}
                      </td>
                      <td className="px-4 py-3 text-right">
                        {org.license_status === 'active' ? (
                          <Button
                            variant="danger"
                            size="sm"
                            onClick={() => handleSuspend(org.id)}
                            disabled={pendingActionId === org.id}
                          >
                            {pendingActionId === org.id ? 'Suspending…' : 'Suspend'}
                          </Button>
                        ) : (
                          <Button
                            size="sm"
                            onClick={() => handleActivate(org.id)}
                            disabled={pendingActionId === org.id}
                          >
                            {pendingActionId === org.id ? 'Activating…' : 'Activate'}
                          </Button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      </section>
    </main>
  )
}
