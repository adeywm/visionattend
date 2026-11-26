import { ArrowDownToLine, ArrowRight, Camera, ChevronLeft, ChevronRight, RefreshCw, Trash2, UserPlus } from 'lucide-react'
import { lazy, Suspense, useEffect, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { api, errorText, formatTime, json, type Page, type Profile, type RecordRow, type Settings } from './api'
import { cameraError, devices, preferredCamera, saveCamera } from './camera'

const AttendanceChart = lazy(() => import('./chart'))

// Wait for typing to pause before querying, instead of fetching on every keystroke.
function useDebounced<T>(value: T, delay = 300) {
  const [settled, setSettled] = useState(value)
  useEffect(() => {
    const timer = window.setTimeout(() => setSettled(value), delay)
    return () => window.clearTimeout(timer)
  }, [value, delay])
  return settled
}

function useResource<T>(path: string, autoRefresh = false) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState('')
  const [revision, refresh] = useState(0)
  useEffect(() => {
    const controller = new AbortController()
    const load = () => api<T>(path, { signal: controller.signal }).then(value => {
      if (!controller.signal.aborted) {
        setData(value)
        setError('')
      }
    }).catch(problem => {
      if (!controller.signal.aborted) setError(errorText(problem))
    })
    setData(null)
    setError('')
    load()
    const timer = autoRefresh ? window.setInterval(load, 20000) : undefined
    return () => {
      controller.abort()
      window.clearInterval(timer)
    }
  }, [path, revision, autoRefresh])
  return { data, error, retry: () => refresh(previous => previous + 1) }
}

export function LoadState({ error, retry }: { error: string; retry: () => void }) {
  return <div className="panel panel-body" role={error ? 'alert' : 'status'}>
    {error ? <><p className="error">{error}</p><button className="button subtle" onClick={retry}>Try again</button></> :
      <p>Loading…</p>}
  </div>
}

type Overview = {
  registered: number; present: number; pending: number; rate: number
  days: { date: string; count: number }[]
  recent: RecordRow[]
}

export function Dashboard({ timezone }: { timezone: string }) {
  const { data, error, retry } = useResource<Overview>('/dashboard', true)
  return <>
    <div className="page-heading page-actions">
      <div><h1>Attendance overview</h1><p>Today’s check-ins and recent activity.</p></div>
      <Link className="button" to="/attendance"><Camera size={17} />Open terminal</Link>
    </div>
    {error || !data ? <LoadState error={error} retry={retry} /> : <>
      <div className="dashboard-metrics">
        {[
          ['Registered profiles', data.registered],
          ['Checked in today', data.present],
          ['Not checked in', data.pending],
          ['Attendance rate', data.rate.toFixed(1) + '%'],
        ].map(([label, value]) => <section className="metric panel" key={label}><p>{label}</p><strong>{value}</strong></section>)}
      </div>
      <div className="dashboard-grid">
        <section className="panel panel-body">
          <h2>Last seven days</h2>
          <p className="muted">Daily check-ins · {timezone}</p>
          <div className="chart" aria-label="Check-ins over the last seven days">
            <Suspense fallback={<p className="muted">Loading chart…</p>}><AttendanceChart days={data.days} /></Suspense>
          </div>
          {data.present === 0 && data.days.every(day => day.count === 0) && <p className="muted">No check-ins in this period.</p>}
        </section>
        <section className="panel panel-body">
          <div className="panel-heading inline"><h2>Recent check-ins</h2><Link className="inline-link" to="/records">View records <ArrowRight size={15} /></Link></div>
          {data.recent.length ? <ul className="events">{data.recent.map(record => <li key={record.public_id + record.checked_in_at}>
            <div><strong>{record.full_name}</strong><small>{record.public_id}</small></div>
            <time dateTime={record.checked_in_at}>{formatTime(record.checked_in_at, timezone, true)}</time>
          </li>)}</ul> : <p className="empty">Check-ins will appear here after confirmation.</p>}
        </section>
      </div>
    </>}
  </>
}

function Pagination({ page, total, setPage }: { page: number; total: number; setPage: (page: number) => void }) {
  const pages = Math.max(1, Math.ceil(total / 20))
  return <div className="pagination">
    <button className="button subtle" disabled={page <= 1} onClick={() => setPage(page - 1)} aria-label="Previous page"><ChevronLeft size={16} /></button>
    <span>Page {page} of {pages} · {total} total</span>
    <button className="button subtle" disabled={page >= pages} onClick={() => setPage(page + 1)} aria-label="Next page"><ChevronRight size={16} /></button>
  </div>
}

export function Users({ timezone }: { timezone: string }) {
  const [search, setSearch] = useState('')
  const [page, setPage] = useState(1)
  const [issue, setIssue] = useState('')
  const [deleting, setDeleting] = useState('')
  const query = useDebounced(search)
  const { data, error, retry } = useResource<Page<Profile>>('/users?' + new URLSearchParams({ search: query, page: String(page) }))

  async function remove(profile: Profile) {
    if (!window.confirm(`Delete ${profile.full_name} and their face template and attendance records? This cannot be undone.`)) return
    setIssue('')
    setDeleting(profile.public_id)
    try {
      await api('/users/' + profile.public_id, { method: 'DELETE' })
      if (page > 1 && data?.items.length === 1) setPage(page - 1)
      else retry()
    } catch (problem) {
      setIssue(errorText(problem))
    } finally {
      setDeleting('')
    }
  }

  return <>
    <div className="page-heading page-actions"><div><h1>Registered users</h1><p>Find a profile, recover an ID, or update a face template.</p></div>
      <Link className="button" to="/register"><UserPlus size={17} />Register a profile</Link>
    </div>
    <div className="filters"><label htmlFor="user-search">Search users
      <input id="user-search" type="search" placeholder="Name, email, or ID" value={search} maxLength={100}
        onChange={event => { setPage(1); setSearch(event.target.value) }} />
    </label></div>
    {issue && <p className="error" role="alert">{issue}</p>}
    {error || !data ? <LoadState error={error} retry={retry} /> : <div className="panel">
      <div className="table-scroll"><table>
        <thead><tr><th scope="col">Profile ID</th><th scope="col">Name</th><th scope="col">Email</th>
          <th scope="col">Registered</th><th scope="col">Enrollment</th><th scope="col">Actions</th></tr></thead>
        <tbody>{data.items.map(profile => <tr key={profile.public_id}>
          <td className="id">{profile.public_id}</td><td>{profile.full_name}</td><td>{profile.email}</td>
          <td>{formatTime(profile.created_at, timezone, true)}</td><td>Enrolled</td>
          <td className="row-actions"><Link className="inline-link" to={'/register?reenroll=' + profile.public_id}><RefreshCw size={14} />Re-enroll</Link>
            <button className="icon-button" disabled={!!deleting} onClick={() => remove(profile)}
              aria-label={'Delete ' + profile.full_name} title="Delete profile"><Trash2 size={16} /></button></td>
        </tr>)}</tbody>
      </table></div>
      {!data.items.length && <p className="empty">{search ? 'No profiles match your search.' : 'No profiles registered yet.'}</p>}
      <Pagination page={page} total={data.total} setPage={setPage} />
    </div>}
  </>
}

export function Records({ timezone }: { timezone: string }) {
  const [form, setForm] = useState({ start: '', end: '', search: '', public_id: '' })
  const [page, setPage] = useState(1)
  const encode = (values: typeof form) => new URLSearchParams(Object.entries(values).filter(([, value]) => value)).toString()
  const query = encode(form)
  const fetched = encode(useDebounced(form))
  const invalid = !!form.start && !!form.end && form.end < form.start
  const { data, error, retry } = useResource<Page<RecordRow>>('/attendance?page=' + page + (fetched ? '&' + fetched : ''))
  function field(key: keyof typeof form, label: string, type: string, placeholder?: string) {
    return <label key={key} htmlFor={key}>{label}<input id={key} type={type} placeholder={placeholder}
      value={form[key]} maxLength={key === 'public_id' ? 19 : 100}
      onChange={event => { setPage(1); setForm({ ...form, [key]: key === 'public_id' ? event.target.value.toUpperCase() : event.target.value }) }} /></label>
  }
  return <>
    <div className="page-heading page-actions"><div><h1>Attendance records</h1><p>Check-ins are shown in {timezone}.</p></div>
      {!invalid && <a className="button subtle" href={'/api/attendance.csv' + (query ? '?' + query : '')} download>
        <ArrowDownToLine size={16} />Export CSV</a>}
    </div>
    <div className="filters">
      {field('start', 'Start date', 'date')}{field('end', 'End date', 'date')}
      {field('public_id', 'Profile ID', 'text', 'VA-000001')}{field('search', 'Search records', 'search', 'Name, email, or ID')}
    </div>
    {invalid ? <p className="error" role="alert">End date must be on or after the start date.</p> :
      error || !data ? <LoadState error={error} retry={retry} /> : <div className="panel">
        <div className="table-scroll"><table>
          <thead><tr><th scope="col">Name</th><th scope="col">Profile ID</th><th scope="col">Date</th>
            <th scope="col">Check-in time</th><th scope="col">Cosine similarity</th></tr></thead>
          <tbody>{data.items.map(record => <tr key={record.public_id + record.attendance_date}>
            <td>{record.full_name}</td><td className="id">{record.public_id}</td><td>{record.attendance_date}</td>
            <td>{formatTime(record.checked_in_at, timezone)}</td><td>{record.similarity.toFixed(3)}</td>
          </tr>)}</tbody>
        </table></div>
        {!data.items.length && <p className="empty">{query ? 'No records match these filters.' : 'No check-ins yet.'}</p>}
        <Pagination page={page} total={data.total} setPage={setPage} />
      </div>}
  </>
}

export function Preferences({ settings, onSave }: { settings: Settings; onSave: (settings: Settings) => void }) {
  const [params] = useSearchParams()
  const [threshold, setThreshold] = useState(String(settings.threshold))
  const [stable, setStable] = useState(String(settings.stable_frames))
  const [available, setAvailable] = useState<MediaDeviceInfo[]>([])
  const [selected, setSelected] = useState(preferredCamera)
  const [status, setStatus] = useState('')
  const [deviceError, setDeviceError] = useState('')
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    if (navigator.mediaDevices) devices().then(setAvailable).catch(() => {})
  }, [])

  async function listDevices() {
    setDeviceError('')
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ video: true, audio: false })
      try {
        setAvailable(await devices())
      } finally {
        stream.getTracks().forEach(track => track.stop())
      }
    } catch (problem) {
      setDeviceError(cameraError(problem))
    }
  }

  async function save(event: React.FormEvent) {
    event.preventDefault()
    setSaving(true)
    setError('')
    setStatus('')
    try {
      onSave(await api<Settings>('/settings', json('PATCH', { threshold: Number(threshold), stable_frames: Number(stable) })))
      setStatus('Verification settings saved.')
    } catch (problem) {
      setError(errorText(problem))
    } finally {
      setSaving(false)
    }
  }

  return <>
    <div className="page-heading"><h1>Settings</h1><p>Camera sources, model status, and verification policy.</p></div>
    <div className="settings-grid">
      <section className="panel panel-body stack">
        <h2>Camera and models</h2>
        <dl className="settings-list">
          <div><dt>YuNet and SFace</dt><dd>{settings.models_ready ? 'Ready' : 'Unavailable'}</dd></div>
          <div><dt>Phone pairing</dt><dd>{settings.phone_enabled ? 'Enabled' : 'Disabled'}</dd></div>
          <div><dt>Attendance timezone</dt><dd>{settings.timezone}</dd></div>
        </dl>
        <label htmlFor="source">Default laptop camera</label>
        <select id="source" value={selected} onChange={event => {
          saveCamera(event.target.value)
          setSelected(event.target.value)
        }}>
          <option value="">System default</option>
          {selected && !available.some(camera => camera.deviceId === selected) && <option value={selected}>Previously selected camera</option>}
          {available.filter(camera => camera.deviceId).map((camera, index) =>
            <option key={camera.deviceId} value={camera.deviceId}>{camera.label || 'Camera ' + (index + 1)}</option>)}
        </select>
        <button className="button subtle" onClick={listDevices}><Camera size={17} />Refresh camera list</button>
        {deviceError && <p className="error" role="alert">{deviceError}</p>}
        <details className="phone-setup" open={params.get('phone') === 'setup' ? true : undefined}>
          <summary>Phone camera setup</summary>
          <p>For a phone app that supplies an RTSP or MJPEG URL, choose <strong>IP camera</strong> on the attendance or enrollment screen. Use the URL shown by the app.</p>
          <p>For QR pairing, put both devices on the same Wi-Fi and follow <strong>Use a phone camera over Wi-Fi</strong> in the project README. Once HTTPS phone mode is running, choose <strong>Pair phone</strong>.</p>
          <Link className="inline-link" to="/attendance">Open attendance terminal <ArrowRight size={15} /></Link>
        </details>
        {!settings.models_ready && <p className="error">Download both models and restart VisionAttend.</p>}
      </section>
      <section className="panel panel-body stack">
        <h2>Verification</h2>
        <form className="stack" onSubmit={save}>
          <label htmlFor="threshold">Minimum cosine similarity</label>
          <input id="threshold" type="number" min="0" max="1" step="0.001" required value={threshold}
            onChange={event => setThreshold(event.target.value)} />
          <p className="muted">Cosine similarity measures how close a live face is to an enrolled template, from −1 to 1. Higher is stricter: fewer wrong matches, more missed check-ins. 0.363 is OpenCV's benchmark default. Try 0.45–0.55 for attendance, and test it with your own people and lighting.</p>
          <label htmlFor="stable">Consecutive matching frames</label>
          <input id="stable" type="number" min="2" max="8" step="1" required value={stable}
            onChange={event => setStable(event.target.value)} />
          <p className="muted">How many frames in a row must match the same person before a check-in is saved. More frames are safer but slower.</p>
          <button className="button" disabled={saving}>Save settings</button>
        </form>
        {status && <p role="status">{status}</p>}{error && <p className="error" role="alert">{error}</p>}
      </section>
    </div>
  </>
}
