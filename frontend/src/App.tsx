import { lazy, Suspense } from 'react'
import { BrowserRouter, Routes, Route, Outlet } from 'react-router-dom'
import Landing from './pages/Landing'
import Terms from './pages/Terms'
import Privacy from './pages/Privacy'
import { LaunchProvider, Protected, AdminProtected } from './lib/auth'
import { ActingUserProvider } from './lib/actingUser'
import { OrgProvider } from './lib/org'
import OrgGate from './components/OrgGate'

// The public landing and legal pages load without the signed-in app's code
// (editor, budgeting, admin), which is fetched only when those routes open.
const JoinOrg = lazy(() => import('./pages/JoinOrg'))
const AdminDashboard = lazy(() => import('./pages/AdminDashboard'))
const SuiteHome = lazy(() => import('./pages/SuiteHome'))
const ProtocolApp = lazy(() => import('./ProtocolApp'))
const BudgetingApp = lazy(() => import('./BudgetingApp'))

function RouteLoading() {
  return (
    <div className="min-h-screen flex items-center justify-center bg-slate-50" role="status" aria-live="polite">
      <span className="text-sm text-slate-500">Loading…</span>
    </div>
  )
}

export default function App() {
  return (
    <BrowserRouter>
      <LaunchProvider>
        <Suspense fallback={<RouteLoading />}>
        <Routes>
          <Route path="/" element={<Landing />} />
          <Route
            path="/app"
            element={
              <Protected>
                <ActingUserProvider>
                  <OrgProvider>
                    <OrgGate>
                      <Outlet />
                    </OrgGate>
                  </OrgProvider>
                </ActingUserProvider>
              </Protected>
            }
          >
            <Route index element={<SuiteHome />} />
            <Route path="protocol" element={<ProtocolApp />} />
            <Route path="budgeting" element={<BudgetingApp />} />
          </Route>
          <Route
            path="/join/:token"
            element={
              <Protected>
                <JoinOrg />
              </Protected>
            }
          />
          <Route path="/terms" element={<Terms />} />
          <Route path="/privacy" element={<Privacy />} />
          <Route
            path="/admin"
            element={
              <AdminProtected>
                <AdminDashboard />
              </AdminProtected>
            }
          />
          {/* Unknown routes fall back to the landing page */}
          <Route path="*" element={<Landing />} />
        </Routes>
        </Suspense>
      </LaunchProvider>
    </BrowserRouter>
  )
}
