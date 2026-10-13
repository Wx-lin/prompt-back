import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import { AuthProvider, useAuth } from './store/auth'
import LoginPage from './pages/LoginPage'
import Layout from './components/Layout'
import PromptsPage from './pages/PromptsPage'
import SubmissionsPage from './pages/SubmissionsPage'
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
          <Route path="/" element={<Navigate to="/prompts" replace />} />
          <Route path="/prompts" element={<PromptsPage />} />
          <Route path="/submissions" element={<SubmissionsPage />} />
          <Route path="/reports" element={<ReportsPage />} />
          <Route path="/crawl-drafts" element={<CrawlDraftsPage />} />
          <Route path="/crawl-schedules" element={<CrawlSchedulesPage />} />
          <Route path="*" element={<Navigate to="/prompts" replace />} />
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
