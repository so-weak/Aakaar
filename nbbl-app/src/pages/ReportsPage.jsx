import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import './ReportsPage.css'

const REPORTS = [
  {
    id: 'biller_transactions',
    title: 'Biller Transactions — May 2026',
    description: 'All BBPS transactions processed via NBBL across categories for the current month.',
    file: 'biller_transactions_2026_05.csv',
    category: 'Transactions',
    rows: 12,
    period: 'May 2026',
    updatedAt: '2026-05-07 09:15 IST'
  },
  {
    id: 'settlement_summary',
    title: 'Settlement Summary — May 2026',
    description: 'Cycle-wise settlement summary by COU including convenience fees and net settlement amounts.',
    file: 'settlement_summary_2026_05.csv',
    category: 'Settlement',
    rows: 12,
    period: 'May 2026',
    updatedAt: '2026-05-07 06:00 IST'
  },
  {
    id: 'biller_master',
    title: 'Biller Master',
    description: 'Master list of all billers onboarded on the NBBL platform with category and coverage.',
    file: 'biller_master.csv',
    category: 'Master Data',
    rows: 15,
    period: 'Current',
    updatedAt: '2026-05-06 22:30 IST'
  },
  {
    id: 'dispute_register',
    title: 'Dispute Register — May 2026',
    description: 'Customer and COU-raised disputes with reason codes, status and TAT tracking.',
    file: 'dispute_register_2026_05.csv',
    category: 'Disputes',
    rows: 7,
    period: 'May 2026',
    updatedAt: '2026-05-07 08:00 IST'
  },
  {
    id: 'agent_performance',
    title: 'Agent Institution Performance',
    description: 'COU-wise volume, success rate, and average TAT across the NBBL network.',
    file: 'agent_institution_performance.csv',
    category: 'Performance',
    rows: 10,
    period: 'YTD',
    updatedAt: '2026-05-06 18:45 IST'
  }
]

function CategoryBadge({ category }) {
  const colors = {
    Transactions: { bg: '#dbeafe', fg: '#1e40af' },
    Settlement: { bg: '#dcfce7', fg: '#15803d' },
    'Master Data': { bg: '#fef3c7', fg: '#a16207' },
    Disputes: { bg: '#fee2e2', fg: '#b91c1c' },
    Performance: { bg: '#ede9fe', fg: '#6d28d9' }
  }
  const c = colors[category] || { bg: '#e2e8f0', fg: '#334155' }
  return (
    <span className="badge" style={{ background: c.bg, color: c.fg }}>
      {category}
    </span>
  )
}

export default function ReportsPage({ onLogout }) {
  const navigate = useNavigate()
  const [search, setSearch] = useState('')
  const [filter, setFilter] = useState('All')
  const [downloading, setDownloading] = useState(null)

  const categories = ['All', ...new Set(REPORTS.map((r) => r.category))]

  const filtered = REPORTS.filter((r) => {
    const matchesSearch = (r.title + ' ' + r.description)
      .toLowerCase()
      .includes(search.toLowerCase())
    const matchesFilter = filter === 'All' || r.category === filter
    return matchesSearch && matchesFilter
  })

  const handleDownload = async (report) => {
    setDownloading(report.id)
    try {
      const res = await fetch(`/reports/${report.file}`)
      if (!res.ok) throw new Error('Failed to fetch file')
      const blob = await res.blob()
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = report.file
      document.body.appendChild(a)
      a.click()
      document.body.removeChild(a)
      URL.revokeObjectURL(url)
    } catch (err) {
      alert('Could not download report. Please try again.')
    } finally {
      setTimeout(() => setDownloading(null), 600)
    }
  }

  const handleLogout = () => {
    onLogout()
    navigate('/')
  }

  return (
    <div className="reports-page">
      <header className="reports-header">
        <div className="header-inner">
          <div className="brand">
            <img src="/logo_nbbl.png" alt="NBBL" className="brand-logo" />
            <div className="brand-divider" />
            <div className="brand-text">
              <div className="brand-title">Reports Portal</div>
              <div className="brand-sub">NPCI Bharat BillPay Ltd.</div>
            </div>
          </div>
          <div className="header-actions">
            <span className="user-chip">
              <span className="user-avatar">A</span>
              <span>admin</span>
            </span>
            <button className="logout-btn" onClick={handleLogout}>Logout</button>
          </div>
        </div>
      </header>

      <main className="reports-main">
        <section className="page-intro">
          <h1>Download Reports</h1>
          <p>
            Access and download the latest NBBL operational reports. All reports
            are exported in CSV format.
          </p>
        </section>

        <section className="reports-toolbar">
          <input
            type="search"
            placeholder="Search reports…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="search-input"
          />
          <div className="filter-pills">
            {categories.map((c) => (
              <button
                key={c}
                className={`pill ${filter === c ? 'pill-active' : ''}`}
                onClick={() => setFilter(c)}
              >
                {c}
              </button>
            ))}
          </div>
        </section>

        <section className="reports-grid">
          {filtered.length === 0 && (
            <div className="empty-state">No reports match your search.</div>
          )}
          {filtered.map((report) => (
            <article key={report.id} className="report-card">
              <div className="report-card-top">
                <CategoryBadge category={report.category} />
                <span className="report-meta-rows">{report.rows} rows</span>
              </div>
              <h3 className="report-title">{report.title}</h3>
              <p className="report-desc">{report.description}</p>
              <div className="report-meta">
                <div>
                  <span className="meta-label">Period</span>
                  <span className="meta-value">{report.period}</span>
                </div>
                <div>
                  <span className="meta-label">Updated</span>
                  <span className="meta-value">{report.updatedAt}</span>
                </div>
              </div>
              <button
                className="download-btn"
                onClick={() => handleDownload(report)}
                disabled={downloading === report.id}
              >
                {downloading === report.id ? (
                  <>Downloading…</>
                ) : (
                  <>
                    <svg
                      width="16"
                      height="16"
                      viewBox="0 0 24 24"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="2.2"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                    >
                      <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
                      <polyline points="7 10 12 15 17 10" />
                      <line x1="12" y1="15" x2="12" y2="3" />
                    </svg>
                    Download CSV
                  </>
                )}
              </button>
            </article>
          ))}
        </section>
      </main>

      <footer className="reports-footer">
        © {new Date().getFullYear()} NPCI Bharat BillPay Ltd. — Internal Use Only
      </footer>
    </div>
  )
}
