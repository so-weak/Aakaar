import { Routes, Route, Navigate } from 'react-router-dom'
import { useState } from 'react'
import LoginPage from './pages/LoginPage.jsx'
import ReportsPage from './pages/ReportsPage.jsx'

export default function App() {
  const [isAuthed, setIsAuthed] = useState(
    () => sessionStorage.getItem('nbbl_auth') === '1'
  )

  const handleLogin = () => {
    sessionStorage.setItem('nbbl_auth', '1')
    setIsAuthed(true)
  }

  const handleLogout = () => {
    sessionStorage.removeItem('nbbl_auth')
    setIsAuthed(false)
  }

  return (
    <Routes>
      <Route
        path="/"
        element={
          isAuthed ? <Navigate to="/reports" replace /> : <LoginPage onLogin={handleLogin} />
        }
      />
      <Route
        path="/reports"
        element={
          isAuthed ? <ReportsPage onLogout={handleLogout} /> : <Navigate to="/" replace />
        }
      />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  )
}
