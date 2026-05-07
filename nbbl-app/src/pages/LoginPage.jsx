import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import './LoginPage.css'

const VALID_USERNAME = 'admin'
const VALID_PASSWORD = 'nbbl@123'
const CAPTCHA_VALUE = 'E5HYVW'

export default function LoginPage({ onLogin }) {
  const navigate = useNavigate()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [captchaInput, setCaptchaInput] = useState('')
  const [error, setError] = useState('')

  const handleSubmit = (e) => {
    e.preventDefault()
    setError('')

    if (!username || !password || !captchaInput) {
      setError('Please fill in all fields.')
      return
    }
    if (captchaInput.toUpperCase() !== CAPTCHA_VALUE) {
      setError('Captcha does not match. Please try again.')
      setCaptchaInput('')
      return
    }
    if (username !== VALID_USERNAME || password !== VALID_PASSWORD) {
      setError('Invalid username or password.')
      setCaptchaInput('')
      return
    }

    onLogin()
    navigate('/reports')
  }

  return (
    <div className="login-page">
      <div className="login-card">
        <div className="login-logo">
          <img src="/logo_nbbl.png" alt="NBBL — NPCI Bharat BillPay Ltd." />
        </div>
        <h1 className="login-title">Reports Portal</h1>
        <p className="login-subtitle">Sign in to access NBBL reports</p>

        <form onSubmit={handleSubmit} className="login-form">
          <label className="field">
            <span className="field-label">Username</span>
            <input
              type="text"
              autoComplete="username"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              placeholder="Enter username"
            />
          </label>

          <label className="field">
            <span className="field-label">Password</span>
            <input
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="Enter password"
            />
          </label>

          <div className="field">
            <span className="field-label">Captcha</span>
            <div className="captcha-row">
              <img
                src="/captcha.png"
                alt="Captcha"
                className="captcha-image"
              />
            </div>
            <input
              type="text"
              value={captchaInput}
              onChange={(e) => setCaptchaInput(e.target.value)}
              placeholder="Enter captcha shown above"
              className="captcha-input"
              maxLength={6}
            />
          </div>

          {error && <div className="login-error">{error}</div>}

          <button type="submit" className="login-button">Sign In</button>

          <p className="login-hint">
            Hint: <code>admin</code> / <code>nbbl@123</code>
          </p>
        </form>
      </div>
      <footer className="login-footer">
        © {new Date().getFullYear()} NPCI Bharat BillPay Ltd. All rights reserved.
      </footer>
    </div>
  )
}
