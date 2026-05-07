import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { CREDENTIALS } from '../data/mockData.js'
import './Login.css'

export default function Login() {
  const navigate = useNavigate()
  const [userId, setUserId] = useState('K22408m')
  const [password, setPassword] = useState('')
  const [showPassword, setShowPassword] = useState(false)
  const [error, setError] = useState('')

  const handleSubmit = (e) => {
    e.preventDefault()
    if (userId === CREDENTIALS.userId && password === CREDENTIALS.password) {
      sessionStorage.setItem('boss_auth', 'true')
      sessionStorage.setItem('boss_user', userId)
      window.dispatchEvent(new Event('boss-auth-change'))
      navigate('/dashboard', { replace: true })
    } else {
      setError('Invalid User ID or Password')
    }
  }

  return (
    <div className="login-page">
      <div
        className="login-hero"
        style={{ backgroundImage: 'url(/images/login-bg.webp)' }}
      />
      <div className="login-panel">
        <div className="login-card">
          <img
            src="/images/hdfc-bank-logo.svg"
            alt="HDFC Bank"
            className="login-logo"
          />
          <h1 className="login-title">
            Welcome to HDFC<br />Bank's Operational<br />Support System
          </h1>

          <form onSubmit={handleSubmit} className="login-form" noValidate>
            <label className="login-label">
              User ID<span className="req">*</span>
            </label>
            <input
              type="text"
              className="login-input"
              value={userId}
              onChange={(e) => setUserId(e.target.value)}
              autoComplete="username"
            />

            <label className="login-label">
              Password<span className="req">*</span>
            </label>
            <div className="login-password-wrap">
              <input
                type={showPassword ? 'text' : 'password'}
                className="login-input"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete="current-password"
              />
              <button
                type="button"
                className="login-eye"
                onClick={() => setShowPassword((s) => !s)}
                aria-label="Toggle password visibility"
              >
                {showPassword ? '\u{1F441}' : '··'}
              </button>
            </div>

            {error && <div className="login-error">{error}</div>}

            <button type="submit" className="login-button">
              Log In
            </button>

            <div className="login-hint">
              Hint &mdash; User ID: <code>{CREDENTIALS.userId}</code> &middot;
              Password: <code>{CREDENTIALS.password}</code>
            </div>
          </form>
        </div>
      </div>
    </div>
  )
}
