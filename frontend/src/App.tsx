import { lazy, Suspense } from 'react'
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import Landing from './pages/Landing'
import Terms from './pages/Terms'
import Privacy from './pages/Privacy'
import { LaunchProvider, Protected, AdminProtected } from './lib/auth'
import { ActingUserProvider } from './lib/actingUser'
import { OrgProvider } from './lib/org'
import OrgGate from './components/OrgGate'
import { AdminFrame } from './shell/AdminFrame'

// The public landing and legal pages load without the signed-in app's code
// (editor, budgeting, admin), which is fetched only when those routes open.
const AppShell = lazy(() => import('./shell/AppShell'))
const JoinOrg = lazy(() => import('./pages/JoinOrg'))
const AdminDashboard = lazy(() => import('./pages/AdminDashboard'))
const Home = lazy(() => import('./pages/app/Home'))
const NewChatPlaceholder = lazy(() => import('./pages/app/NewChatPlaceholder'))
const ProtocolsPage = lazy(() => import('./pages/app/ProtocolsPage'))
const ProtocolEditorPage = lazy(() => import('./pages/app/ProtocolEditorPage'))
const CsrPage = lazy(() => import('./pages/app/CsrPage'))
const BudgetsPage = lazy(() => import('./pages/app/BudgetsPage'))
const BudgetEditorPage = lazy(() => import('./pages/app/BudgetEditorPage'))
const StudyReportsPage = lazy(() => import('./pages/app/StudyReportsPage'))
const TeamPage = lazy(() => import('./pages/app/TeamPage'))
// The classic wizards stay mounted inside the shell (`embedded`: no header of
// their own) until the chat UI replaces them.
const ProtocolApp = lazy(() => import('./ProtocolApp'))
const BudgetingApp = lazy(() => import('./BudgetingApp'))

function RouteLoading() {
  return (
    <div className="min-h-screen flex items-center justify-center bg-surface" role="status" aria-live="polite">
      <span className="text-small text-fg-subtle">Loading…</span>
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
                      <AppShell />
                    </OrgGate>
                  </OrgProvider>
                </ActingUserProvider>
              </Protected>
            }
          >
            <Route index element={<Home />} />
            <Route path="chat/new" element={<NewChatPlaceholder />} />
            <Route path="chat/:sessionId" element={<NewChatPlaceholder />} />
            <Route path="protocols" element={<ProtocolsPage />} />
            <Route path="protocols/:id" element={<ProtocolEditorPage />} />
            <Route path="protocols/:id/report" element={<CsrPage />} />
            <Route path="budgets" element={<BudgetsPage />} />
            <Route path="budgets/:id" element={<BudgetEditorPage />} />
            <Route path="reports" element={<StudyReportsPage />} />
            <Route path="team" element={<TeamPage />} />
            <Route path="protocol" element={<ProtocolApp embedded />} />
            <Route path="budgeting" element={<BudgetingApp embedded />} />
            <Route path="*" element={<Navigate to="/app" replace />} />
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
                <AdminFrame>
                  <AdminDashboard />
                </AdminFrame>
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
