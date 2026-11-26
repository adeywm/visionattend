import { Camera, ClipboardList, LayoutDashboard, ScanFace, Settings2, UserPlus, Users as UsersIcon } from 'lucide-react'
import { StrictMode, useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter, Link, NavLink, Route, Routes, useLocation } from 'react-router-dom'
import { api, errorText, type Settings } from './api'
import { Dashboard, LoadState, Preferences, Records, Users } from './pages'
import { PhoneCamera } from './phone'
import { Register } from './register'
import { Terminal } from './terminal'
import './style.css'

const navigation = [
  ['/', 'Overview', LayoutDashboard],
  ['/register', 'Register', UserPlus],
  ['/attendance', 'Attendance', Camera],
  ['/users', 'Users', UsersIcon],
  ['/records', 'Records', ClipboardList],
  ['/settings', 'Settings', Settings2],
] as const

function Desktop() {
  const [settings, setSettings] = useState<Settings | null>(null)
  const [error, setError] = useState('')
  const [revision, retry] = useState(0)
  const location = useLocation()

  useEffect(() => {
    const controller = new AbortController()
    setError('')
    api<Settings>('/settings', { signal: controller.signal }).then(setSettings).catch(problem => {
      if (!controller.signal.aborted) setError(errorText(problem))
    })
    return () => controller.abort()
  }, [revision])

  return <div className="shell">
    <a className="skip" href="#main">Skip to content</a>
    <aside className="sidebar">
      <Link to="/" className="brand"><ScanFace size={25} aria-hidden="true" />VisionAttend</Link>
      <nav aria-label="Main navigation">{navigation.map(([url, text, Icon]) =>
        <NavLink to={url} key={url} end={url === '/'}><Icon size={18} aria-hidden="true" />{text}</NavLink>
      )}</nav>
      <div className="sidebar-note">Local demo · Consenting participants only</div>
    </aside>
    <main id="main" className="workspace">
      {!settings ? <LoadState error={error} retry={() => retry(value => value + 1)} /> :
        <Routes>
          <Route path="/" element={<Dashboard timezone={settings.timezone} />} />
          <Route path="/register" element={<Register key={location.search} phoneEnabled={settings.phone_enabled} />} />
          <Route path="/attendance" element={<Terminal settings={settings} />} />
          <Route path="/users" element={<Users timezone={settings.timezone} />} />
          <Route path="/records" element={<Records timezone={settings.timezone} />} />
          <Route path="/settings" element={<Preferences settings={settings} onSave={setSettings} />} />
          <Route path="*" element={<div className="page-heading"><h1>Page not found</h1><Link to="/">Back to overview</Link></div>} />
        </Routes>}
    </main>
  </div>
}

createRoot(document.getElementById('root')!).render(<StrictMode>
  {location.pathname === '/phone' ? <PhoneCamera /> : <BrowserRouter><Desktop /></BrowserRouter>}
</StrictMode>)
