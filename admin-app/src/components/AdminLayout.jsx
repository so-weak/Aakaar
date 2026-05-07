import { useState } from 'react'
import { NavLink, Outlet, useNavigate } from 'react-router-dom'
import { LAST_LOGIN } from '../data/mockData.js'
import './AdminLayout.css'

const RECON_SUB_ITEMS = [
  { label: 'DGST Report', to: '/recon/dgst' },
  { label: 'Force Match', to: '/recon/force-match' },
  { label: 'GEFU', to: '/recon/gefu' },
  { label: 'Recon Debit', to: '/recon/debit' },
  { label: 'Recon Refund', to: '/recon/refund' },
  { label: 'Recon Reports', to: '/recon/reports' },
  { label: 'Recon System Date', to: '/recon/system-date' },
  { label: 'Recon Upload', to: '/recon/upload' }
]

export default function AdminLayout() {
  const navigate = useNavigate()
  const [maintenanceOpen, setMaintenanceOpen] = useState(false)
  const [reconOpen, setReconOpen] = useState(true)

  const user = sessionStorage.getItem('boss_user') || 'K'
  const initial = user.charAt(0).toUpperCase()

  const logout = () => {
    sessionStorage.removeItem('boss_auth')
    sessionStorage.removeItem('boss_user')
    window.dispatchEvent(new Event('boss-auth-change'))
    navigate('/login', { replace: true })
  }

  return (
    <div className="admin-shell">
      <aside className="admin-sidebar">
        <div className="admin-sidebar-logo">
          <img src="/images/hdfc-bank-logo.svg" alt="HDFC BANK" />
          <span className="admin-sidebar-tag">BOSS</span>
        </div>

        <nav className="admin-nav">
          <NavLink
            to="/dashboard"
            className={({ isActive }) =>
              'admin-nav-item' + (isActive ? ' active' : '')
            }
          >
            <span className="admin-nav-icon">&#9783;</span>
            Dashboard
          </NavLink>

          <button
            className={'admin-nav-item admin-nav-toggle' + (maintenanceOpen ? ' open' : '')}
            onClick={() => setMaintenanceOpen((v) => !v)}
            type="button"
          >
            <span className="admin-nav-icon">&#9881;</span>
            Maintenance
            <span className="admin-nav-caret">{maintenanceOpen ? '−' : '+'}</span>
          </button>
          {maintenanceOpen && (
            <div className="admin-nav-sub">
              <span className="admin-nav-subitem">Master Maintenance</span>
              <span className="admin-nav-subitem">User Management</span>
              <span className="admin-nav-subitem">Audit Trail</span>
            </div>
          )}

          <button
            className={'admin-nav-item admin-nav-toggle' + (reconOpen ? ' open' : '')}
            onClick={() => setReconOpen((v) => !v)}
            type="button"
          >
            <span className="admin-nav-icon">&#9881;</span>
            Recon &amp; Settlement
            <span className="admin-nav-caret">{reconOpen ? '−' : '+'}</span>
          </button>
          {reconOpen && (
            <div className="admin-nav-sub">
              {RECON_SUB_ITEMS.map((item) => (
                <NavLink
                  key={item.to}
                  to={item.to}
                  className={({ isActive }) =>
                    'admin-nav-subitem' + (isActive ? ' active' : '')
                  }
                >
                  {item.label}
                </NavLink>
              ))}
            </div>
          )}
        </nav>
      </aside>

      <div className="admin-main">
        <header className="admin-header">
          <div className="admin-header-title">
            Bank's Operational Support System
          </div>
          <div className="admin-header-right">
            <span className="admin-last-login">
              Last Login: {LAST_LOGIN}
            </span>
            <button className="admin-icon-btn" title="Settings">&#9881;</button>
            <button className="admin-icon-btn" title="Notifications">&#128276;</button>
            <button className="admin-avatar" onClick={logout} title="Logout">
              {initial}
            </button>
          </div>
        </header>

        <main className="admin-content">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
