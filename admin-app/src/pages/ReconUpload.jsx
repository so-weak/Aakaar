import { useEffect, useRef, useState } from 'react'
import {
  SWITCH_TYPES,
  CYCLE_NUMBERS
} from '../data/mockData.js'
import './ReconUpload.css'

export default function ReconUpload() {
  const [tab, setTab] = useState('upload')
  const [switchType, setSwitchType] = useState('')
  const [skipRecon, setSkipRecon] = useState('No')
  const [cycle, setCycle] = useState('C02')
  const [date, setDate] = useState('2026-05-06')
  const [fileName, setFileName] = useState('')
  const [file, setFile] = useState(null)
  const [uploading, setUploading] = useState(false)
  const [errorMsg, setErrorMsg] = useState('')
  const [history, setHistory] = useState([])
  const [historyError, setHistoryError] = useState('')
  const fileRef = useRef(null)

  const handleBrowse = () => fileRef.current?.click()
  const handleFile = (e) => {
    const f = e.target.files?.[0]
    if (f) {
      setFile(f)
      setFileName(f.name)
    }
  }

  const fetchHistory = async () => {
    try {
      const res = await fetch('/api/recon/uploads')
      if (!res.ok) throw new Error(`history fetch failed: ${res.status}`)
      const data = await res.json()
      setHistory(data)
      setHistoryError('')
    } catch (err) {
      setHistoryError(err.message || String(err))
    }
  }

  // Refresh history whenever the View tab is opened. The backend keeps
  // its list in memory, so this is the single source of truth.
  useEffect(() => {
    if (tab === 'view') fetchHistory()
  }, [tab])

  const handleUpload = async (e) => {
    e.preventDefault()
    setErrorMsg('')
    if (!switchType || !file) {
      setErrorMsg('Please choose a Switch Type and a file before uploading.')
      return
    }
    setUploading(true)
    try {
      const fd = new FormData()
      fd.append('file', file)
      fd.append('switch_type', switchType)
      fd.append('cycle', cycle)
      fd.append('date', date)
      fd.append('skip_recon', skipRecon)

      const res = await fetch('/api/recon/uploads', {
        method: 'POST',
        body: fd
      })
      if (!res.ok) {
        const detail = await res.text()
        throw new Error(`upload failed (${res.status}): ${detail}`)
      }
      const row = await res.json()
      setHistory((prev) => [row, ...prev])
      // Reset file but keep the form's switch/cycle/date so the operator
      // can upload several files in a row without reselecting.
      setFile(null)
      setFileName('')
      if (fileRef.current) fileRef.current.value = ''
      // Jump to the View tab so they see the new row land.
      setTab('view')
    } catch (err) {
      setErrorMsg(err.message || String(err))
    } finally {
      setUploading(false)
    }
  }

  const formatDate = (iso) => {
    if (!iso) return ''
    const [y, m, d] = iso.split('-')
    return `${d}/${m}/${y}`
  }

  return (
    <div className="recon-upload">
      <h2 className="recon-title">Recon Upload Files</h2>

      <div className="recon-tabs">
        <button
          className={'recon-tab' + (tab === 'view' ? ' active' : '')}
          onClick={() => setTab('view')}
        >
          View
        </button>
        <button
          className={'recon-tab' + (tab === 'upload' ? ' active' : '')}
          onClick={() => setTab('upload')}
        >
          Upload
        </button>
      </div>

      {tab === 'upload' ? (
        <form className="recon-form" onSubmit={handleUpload}>
          <div className="recon-row">
            <div className="recon-field recon-field-full">
              <label className="recon-label">
                Switch Type<span className="req">*</span>
              </label>
              <select
                className="recon-input"
                value={switchType}
                onChange={(e) => setSwitchType(e.target.value)}
              >
                <option value="">Select Switch Type</option>
                {SWITCH_TYPES.map((s) => (
                  <option key={s} value={s}>{s}</option>
                ))}
              </select>
            </div>
          </div>

          <div className="recon-row">
            <div className="recon-field recon-field-full">
              <label className="recon-label">
                Skip Recon<span className="req">*</span>
              </label>
              <div className="recon-radio-group">
                <label className="recon-radio">
                  <input
                    type="radio"
                    name="skipRecon"
                    value="Yes"
                    checked={skipRecon === 'Yes'}
                    onChange={() => setSkipRecon('Yes')}
                  />
                  <span>Yes</span>
                </label>
                <label className="recon-radio">
                  <input
                    type="radio"
                    name="skipRecon"
                    value="No"
                    checked={skipRecon === 'No'}
                    onChange={() => setSkipRecon('No')}
                  />
                  <span>No</span>
                </label>
              </div>
            </div>
          </div>

          <div className="recon-row recon-row-2">
            <div className="recon-field">
              <label className="recon-label">
                Cycle Number<span className="req">*</span>
              </label>
              <select
                className="recon-input"
                value={cycle}
                onChange={(e) => setCycle(e.target.value)}
              >
                {CYCLE_NUMBERS.map((c) => (
                  <option key={c} value={c}>{c}</option>
                ))}
              </select>
            </div>
            <div className="recon-field">
              <label className="recon-label">
                Select Date<span className="req">*</span>
              </label>
              <input
                type="date"
                className="recon-input"
                value={date}
                onChange={(e) => setDate(e.target.value)}
              />
            </div>
          </div>

          <div className="recon-row">
            <div className="recon-field recon-field-grow">
              <label className="recon-label">Select a File to Upload</label>
              <input
                type="text"
                className="recon-input"
                placeholder="File_Name"
                value={fileName}
                readOnly
              />
            </div>
            <div className="recon-field recon-field-browse">
              <label className="recon-label">&nbsp;</label>
              <button
                type="button"
                className="recon-browse"
                onClick={handleBrowse}
              >
                Browse
              </button>
              <input
                ref={fileRef}
                type="file"
                accept=".csv,.zip"
                style={{ display: 'none' }}
                onChange={handleFile}
              />
              <span className="recon-supported">
                Supported formats: .csv, .zip
              </span>
            </div>
          </div>

          <div className="recon-actions">
            <button
              type="submit"
              className="recon-upload-btn"
              disabled={uploading}
            >
              {uploading ? 'Uploading…' : '⤒ Upload'}
            </button>
          </div>

          {errorMsg ? (
            <div className="recon-error">{errorMsg}</div>
          ) : null}

          <div className="recon-summary">
            <strong>Summary:</strong> {switchType || '—'} ·{' '}
            Skip: {skipRecon} · Cycle: {cycle} · Date: {formatDate(date)} ·{' '}
            File: {fileName || '—'}
          </div>
        </form>
      ) : (
        <div className="recon-table-wrap">
          {historyError ? (
            <div className="recon-error">
              Couldn't load history: {historyError}
            </div>
          ) : null}
          <table className="recon-table">
            <thead>
              <tr>
                <th>#</th>
                <th>File Name</th>
                <th>Switch Type</th>
                <th>Cycle</th>
                <th>Date</th>
                <th>Status</th>
                <th>Uploaded At</th>
              </tr>
            </thead>
            <tbody>
              {history.length === 0 && !historyError ? (
                <tr>
                  <td colSpan={7} className="recon-empty">
                    No uploads yet.
                  </td>
                </tr>
              ) : (
                history.map((row, i) => (
                  <tr key={row.id}>
                    <td>{i + 1}</td>
                    <td>{row.fileName}</td>
                    <td>{row.switchType}</td>
                    <td>{row.cycle}</td>
                    <td>{row.date}</td>
                    <td>
                      <span
                        className={
                          'recon-status recon-status-' +
                          row.status.toLowerCase()
                        }
                      >
                        {row.status}
                      </span>
                    </td>
                    <td>{row.uploadedAt}</td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
