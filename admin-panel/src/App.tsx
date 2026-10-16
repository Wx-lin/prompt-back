import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import { AuthProvider, useAuth } from './store/auth'
import DashboardPage from './pages/DashboardPage'
import UsersPage from './pages/UsersPage'
import LoginPage from './pages/LoginPage'
import Layout from './components/Layout'
import PromptsPage from './pages/PromptsPage'
import SubmissionsPage from './pages/SubmissionsPage'
import HomepagePage from './pages/HomepagePage'
import ReportsPage from './pages/ReportsPage'
import CrawlDraftsPage from './pages/CrawlDraftsPage'
import CrawlSchedulesPage from './pages/CrawlSchedulesPage'

function AppRoutes() {
  const { isAuthenticated } = useAuth()

  if (!isAuthenticated) {
    return <LoginPage />
  }

  return (
    <BrowserRouter basename={import.meta.env.BASE_URL}>
      <Layout>
        <Routes>
          <Route path="/" element={<Navigate to="/dashboard" replace />} />
          <Route path="/dashboard" element={<DashboardPage />} />
          <Route path="/users" element={<UsersPage />} />
          <Route path="/prompts" element={<PromptsPage />} />
          <Route path="/submissions" element={<SubmissionsPage />} />
          <Route path="/homepage" element={<HomepagePage />} />
          <Route path="/reports" element={<ReportsPage />} />
          <Route path="/crawl-drafts" element={<CrawlDraftsPage />} />
          <Route path="/crawl-schedules" element={<CrawlSchedulesPage />} />
          <Route path="*" element={<Navigate to="/dashboard" replace />} />
        </Routes>
      </Layout>
    </BrowserRouter>
  )
}

export default function App() {
  return (
    <AuthProvider>
      <AppRoutes />
    </AuthProvider>
  )
}
