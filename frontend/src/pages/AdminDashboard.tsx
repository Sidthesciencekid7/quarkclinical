import { useEffect, useState } from 'react'
import { useClerk } from '@clerk/clerk-react'
import { api } from '../api/client'
import type { AdminOrgRow, Lead } from '../types'

/** Operator-only dashboard (ADMIN_EMAILS-gated on the backend) — the
 *  "requests made by organisations" view: Contact Sales leads and
 *  self-serve orgs waiting on manual license activation. Not linked from
 *  anywhere in the regular UI; the backend is the source of truth on
 *  access, so this page just tries the fetch and shows "Access denied" on
 *  a 403 rather than duplicating the ADMIN_EMAILS check client-side. */
export default function AdminDashboard() {
  const clerk = useClerk()
  const [leads, setLeads] = useState<Lead[] | null>(null)
  const [orgs, setOrgs] = useState<AdminOrgRow[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [pendingActionId, setPendingActionId] = useState<string | null>(null)

  const handleLogout = () => {
    clerk.signOut({ redirectUrl: '/admin' })
  }

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

  if (loading) {
    return (
      <div className="app-canvas min-h-screen flex items-center justify-center">
        <span className="text-sm text-slate-400 font-medium">Loading…</span>
      </div>
    )
  }

  if (error) {
    return (
      <div className="app-canvas min-h-screen flex items-center justify-center px-4">
        <div className="bg-white rounded-2xl border border-slate-200/80 shadow-xl max-w-md w-full p-8 text-center">
          <h1 className="text-lg font-bold text-slate-900 mb-2">Access denied</h1>
          <p className="text-sm text-slate-500 leading-relaxed mb-6">{error}</p>
          <button
            type="button"
            onClick={handleLogout}
            className="text-sm font-semibold text-slate-500 hover:text-slate-900 border border-slate-200 hover:border-slate-300 px-4 py-2 rounded-xl cursor-pointer transition-colors"
          >
            Log out
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="app-canvas min-h-screen px-4 py-10">
      <div className="max-w-5xl mx-auto space-y-10">
        <div className="flex items-center justify-between">
          <h1 className="text-2xl font-extrabold text-slate-900">Admin dashboard</h1>
          <button
            type="button"
            onClick={handleLogout}
            className="text-sm font-semibold text-slate-500 hover:text-slate-900 border border-slate-200 hover:border-slate-300 bg-white px-4 py-2 rounded-xl cursor-pointer transition-colors"
          >
            Log out
          </button>
        </div>

        <section>
          <h2 className="text-sm font-bold text-slate-500 uppercase tracking-widest mb-3">
            Contact sales leads ({leads?.length ?? 0})
          </h2>
          <div className="bg-white rounded-2xl border border-slate-200/80 shadow-sm overflow-hidden">
            {!leads || leads.length === 0 ? (
              <p className="p-6 text-sm text-slate-400">No leads yet.</p>
            ) : (
              <table className="w-full text-sm">
                <thead className="bg-slate-50 text-left text-xs font-bold text-slate-500 uppercase tracking-wider">
                  <tr>
                    <th className="px-4 py-3">Company</th>
                    <th className="px-4 py-3">Contact</th>
                    <th className="px-4 py-3">Team size</th>
                    <th className="px-4 py-3">Message</th>
                    <th className="px-4 py-3">Status</th>
                    <th className="px-4 py-3">Received</th>
                    <th className="px-4 py-3"></th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {leads.map(lead => (
                    <tr key={lead.id}>
                      <td className="px-4 py-3 font-semibold text-slate-900">{lead.company_name}</td>
                      <td className="px-4 py-3 text-slate-600">
                        {lead.contact_name}
                        <div className="text-xs text-slate-400">{lead.contact_email}</div>
                      </td>
                      <td className="px-4 py-3 text-slate-600">{lead.team_size_estimate || '—'}</td>
                      <td className="px-4 py-3 text-slate-500 max-w-xs truncate">{lead.message || '—'}</td>
                      <td className="px-4 py-3">
                        <span className="tag bg-slate-100 border-slate-200 text-slate-600">{lead.status}</span>
                      </td>
                      <td className="px-4 py-3 text-slate-400 text-xs">
                        {new Date(lead.created_at).toLocaleString()}
                      </td>
                      <td className="px-4 py-3">
                        <button
                          type="button"
                          onClick={() => handleDeleteLead(lead.id)}
                          disabled={pendingActionId === lead.id}
                          className="text-xs font-semibold text-red-600 hover:text-red-700 disabled:opacity-50 cursor-pointer"
                        >
                          {pendingActionId === lead.id ? 'Deleting…' : 'Delete'}
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </section>

        <section>
          <h2 className="text-sm font-bold text-slate-500 uppercase tracking-widest mb-3">
            Organizations ({orgs?.length ?? 0})
          </h2>
          <div className="bg-white rounded-2xl border border-slate-200/80 shadow-sm overflow-hidden">
            {!orgs || orgs.length === 0 ? (
              <p className="p-6 text-sm text-slate-400">No organizations yet.</p>
            ) : (
              <table className="w-full text-sm">
                <thead className="bg-slate-50 text-left text-xs font-bold text-slate-500 uppercase tracking-wider">
                  <tr>
                    <th className="px-4 py-3">Organization</th>
                    <th className="px-4 py-3">Owner</th>
                    <th className="px-4 py-3">License</th>
                    <th className="px-4 py-3">Days left</th>
                    <th className="px-4 py-3">Seats</th>
                    <th className="px-4 py-3">Created</th>
                    <th className="px-4 py-3"></th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {orgs.map(org => (
                    <tr key={org.id}>
                      <td className="px-4 py-3 font-semibold text-slate-900">
                        {org.name}
                        <div className="text-xs text-slate-400">{org.slug}</div>
                      </td>
                      <td className="px-4 py-3 text-slate-600">{org.owner_email || '—'}</td>
                      <td className="px-4 py-3">
                        <span className={[
                          'tag',
                          org.license_status === 'active' ? 'bg-emerald-50 border-emerald-200 text-emerald-700'
                            : org.license_status === 'pending' ? 'bg-amber-50 border-amber-200 text-amber-700'
                            : 'bg-slate-100 border-slate-200 text-slate-600',
                        ].join(' ')}>
                          {org.license_status || 'none'}
                        </span>
                      </td>
                      <td className="px-4 py-3 text-slate-600">
                        {org.days_remaining === null
                          ? '—'
                          : org.days_remaining < 0
                            ? <span className="text-red-600 font-semibold">Expired</span>
                            : `${org.days_remaining}d`}
                      </td>
                      <td className="px-4 py-3 text-slate-600">{org.seats_used}/{org.seat_count ?? '—'}</td>
                      <td className="px-4 py-3 text-slate-400 text-xs">
                        {new Date(org.created_at).toLocaleString()}
                      </td>
                      <td className="px-4 py-3">
                        {org.license_status === 'active' ? (
                          <button
                            type="button"
                            onClick={() => handleSuspend(org.id)}
                            disabled={pendingActionId === org.id}
                            className="text-xs font-bold text-white bg-red-500 hover:bg-red-600 disabled:opacity-50 px-3 py-1.5 rounded-lg cursor-pointer transition-colors"
                          >
                            {pendingActionId === org.id ? 'Suspending…' : 'Suspend'}
                          </button>
                        ) : (
                          <button
                            type="button"
                            onClick={() => handleActivate(org.id)}
                            disabled={pendingActionId === org.id}
                            className="text-xs font-bold text-white bg-cyan-600 hover:bg-cyan-700 disabled:opacity-50 px-3 py-1.5 rounded-lg cursor-pointer transition-colors"
                          >
                            {pendingActionId === org.id ? 'Activating…' : 'Activate'}
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </section>
      </div>
    </div>
  )
}
