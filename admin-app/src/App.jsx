import { Routes, Route, Navigate, useLocation } from 'react-router-dom'
import { useState, useEffect } from 'react'
import Login from './pages/Login.jsx'
import Dashboard from './pages/Dashboard.jsx'
import ReconUpload from './pages/ReconUpload.jsx'
import AdminLayout from './components/AdminLayout.jsx'

function RequireAuth({ children }) {
  const location = useLocation()
  const isAuthed = sessionStorage.getItem('boss_auth') === 'true'
  if (!isAuthed) return <Navigate to="/login" replace state={{ from: location }} />
  return children
}

export default function App() {
  const [, setTick] = useState(0)
  useEffect(() => {
    const handler = () => setTick((t) => t + 1)
    window.addEventListener('boss-auth-change', handler)
    return () => window.removeEventListener('boss-auth-change', handler)
  }, [])

  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route
        element={
          <RequireAuth>
            <AdminLayout />
          </RequireAuth>
        }
      >
        <Route path="/dashboard" element={<Dashboard />} />
        <Route path="/recon/upload" element={<ReconUpload />} />
      </Route>
      <Route path="*" element={<Navigate to="/dashboard" replace />} />
    </Routes>
  )
}
