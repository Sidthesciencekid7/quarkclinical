import { BrowserRouter, Routes, Route } from 'react-router-dom'
import Landing from './pages/Landing'
import JoinOrg from './pages/JoinOrg'
import Terms from './pages/Terms'
import Privacy from './pages/Privacy'
import AdminDashboard from './pages/AdminDashboard'
import ProtocolApp from './ProtocolApp'
import { LaunchProvider, Protected, AdminProtected } from './lib/auth'
import { ActingUserProvider } from './lib/actingUser'
import { OrgProvider } from './lib/org'
import OrgGate from './components/OrgGate'

export default function App() {
  return (
    <BrowserRouter>
      <LaunchProvider>
        <Routes>
          <Route path="/" element={<Landing />} />
          <Route
            path="/app"
            element={
              <Protected>
                <ActingUserProvider>
                  <OrgProvider>
                    <OrgGate>
                      <ProtocolApp />
                    </OrgGate>
                  </OrgProvider>
                </ActingUserProvider>
              </Protected>
            }
          />
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
      </LaunchProvider>
    </BrowserRouter>
  )
}
