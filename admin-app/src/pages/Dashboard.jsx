import { useNavigate } from 'react-router-dom'
import { DASHBOARD_METRICS } from '../data/mockData.js'
import './Dashboard.css'

const CARDS = [
  {
    key: 'pending',
    label: 'Pending Requests',
    tone: 'amber',
    icon: '⧖'
  },
  {
    key: 'preApproved',
    label: 'Pre Approved Requests',
    tone: 'teal',
    icon: '✅'
  },
  {
    key: 'approved',
    label: 'Approved Requests',
    tone: 'green',
    icon: '✓'
  },
  {
    key: 'rejected',
    label: 'Rejected Requests',
    tone: 'red',
    icon: '✗'
  }
]

export default function Dashboard() {
  const navigate = useNavigate()

  return (
    <div className="dashboard">
      <h2 className="dashboard-title">Dashboard</h2>

      <div className="dashboard-grid">
        {CARDS.map((card) => (
          <div key={card.key} className={'dash-card dash-card-' + card.tone}>
            <div className="dash-card-row">
              <div>
                <div className="dash-card-value">
                  {DASHBOARD_METRICS[card.key]}
                </div>
                <div className="dash-card-label">{card.label}</div>
                <button
                  className="dash-card-button"
                  onClick={() => navigate('/recon/upload')}
                >
                  View Requests
                </button>
              </div>
              <div className="dash-card-icon">{card.icon}</div>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}
