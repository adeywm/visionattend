import { CheckCircle2, Copy } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { api, errorText, guidanceFor, json, type Box, type Profile } from './api'
import { CameraPanel, useCamera } from './camera'

type Enrollment = { session_id: string; required: number; expires_in: number }
type Sample = { count: number; required: number; quality: string; box?: Box | null }

export function Register({ phoneEnabled }: { phoneEnabled: boolean }) {
  const [params] = useSearchParams()
  const replacement = params.get('reenroll') ?? ''
  const camera = useCamera()
  const [session, setSession] = useState<Enrollment | null>(null)
  const active = useRef<Enrollment | null>(null)
  const detailsRequest = useRef<AbortController | null>(null)
  const [sample, setSample] = useState<Sample | null>(null)
  const [profile, setProfile] = useState<Profile | null>(null)
  const [existing, setExisting] = useState<Profile | null>(null)
  const [name, setName] = useState('')
  const [email, setEmail] = useState('')
  const [consent, setConsent] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [copied, setCopied] = useState('')

  useEffect(() => {
    if (!replacement) return
    const controller = new AbortController()
    api<Profile>('/users/' + encodeURIComponent(replacement), { signal: controller.signal })
      .then(setExisting).catch(problem => {
        if (!controller.signal.aborted) setError(errorText(problem))
      })
    return () => controller.abort()
  }, [replacement])

  function discard() {
    const current = active.current
    active.current = null
    if (current) api('/enrollment/' + current.session_id, { method: 'DELETE', keepalive: true }).catch(() => {})
  }

  useEffect(() => {
    window.addEventListener('pagehide', discard)
    return () => {
      detailsRequest.current?.abort()
      window.removeEventListener('pagehide', discard)
      discard()
    }
  }, [])

  async function begin(event: React.FormEvent) {
    event.preventDefault()
    setError('')
    setBusy(true)
    const controller = detailsRequest.current = new AbortController()
    try {
      const body = replacement ? { public_id: replacement, consent } : { full_name: name, email, consent }
      const started = await api<Enrollment>('/enrollment/start', { ...json('POST', body), signal: controller.signal })
      active.current = started
      setSession(started)
    } catch (problem) {
      if (!controller.signal.aborted) setError(errorText(problem))
    } finally {
      if (!controller.signal.aborted) setBusy(false)
    }
  }

  const ready = !!session && (sample?.count ?? 0) >= session.required
  const capture = camera.frame
  const stopCamera = camera.stop
  useEffect(() => {
    if (!session || ready || camera.status !== 'live') return
    const controller = new AbortController()
    let running = false
    const timer = window.setInterval(async () => {
      if (running || controller.signal.aborted) return
      running = true
      try {
        const blob = await capture()
        if (!blob || controller.signal.aborted) return
        const form = new FormData()
        form.append('image', blob, 'capture.jpg')
        const response = await api<Sample>('/enrollment/' + session.session_id + '/sample', {
          method: 'POST', body: form, signal: controller.signal,
        })
        if (!controller.signal.aborted) setSample(response)
      } catch (problem) {
        if (!controller.signal.aborted) {
          setError(errorText(problem))
          stopCamera()
        }
      } finally {
        running = false
      }
    }, 450)
    return () => {
      controller.abort()
      window.clearInterval(timer)
    }
  }, [session, ready, camera.status, capture, stopCamera])

  useEffect(() => {
    if (!session) return
    const timer = window.setTimeout(() => {
      stopCamera()
      discard()
      setSession(null)
      setSample(null)
      setError('Enrollment expired. Start again.')
    }, session.expires_in * 1000)
    return () => window.clearTimeout(timer)
  }, [session, stopCamera])

  function cancel() {
    discard()
    setSession(null)
    setSample(null)
    setError('')
    camera.stop()
  }

  async function complete() {
    if (!session || !ready) return
    setBusy(true)
    setError('')
    const controller = detailsRequest.current = new AbortController()
    try {
      const enrolled = await api<Profile>('/enrollment/' + session.session_id + '/complete', {
        method: 'POST', signal: controller.signal,
      })
      active.current = null
      camera.stop()
      setSession(null)
      setProfile(enrolled)
    } catch (problem) {
      if (!controller.signal.aborted) setError(errorText(problem))
    } finally {
      if (!controller.signal.aborted) setBusy(false)
    }
  }

  return <>
    <div className="page-heading">
      <h1>{replacement ? 'Re-enroll face' : 'Register a profile'}</h1>
      <p>Capture five samples to create a local face template.</p>
    </div>
    <ol className="steps">{['Details', 'Face enrollment', 'Complete'].map((step, index) =>
      <li key={step} aria-current={(profile ? 2 : session ? 1 : 0) === index ? 'step' : undefined}>{step}</li>)}</ol>
    {profile ? <div className="panel panel-body receipt" role="status">
      <CheckCircle2 size={32} aria-hidden="true" />
      <h2>{replacement ? 'Face template updated' : 'Profile registered'}</h2>
      <p>{profile.full_name}</p>
      <strong className="assigned-id">{profile.public_id}</strong>
      <div className="actions">
        <button className="button subtle" onClick={() => navigator.clipboard.writeText(profile.public_id)
          .then(() => setCopied('ID copied')).catch(() => setCopied('Copy unavailable. Select the ID above.'))}><Copy size={16} />Copy ID</button>
        <Link className="button" to="/attendance">Go to attendance</Link>
      </div>
      {copied && <p role="status">{copied}</p>}
    </div> : !session ? <section className="panel panel-body narrow">
      <h2>Profile details</h2>
      <form className="stack" onSubmit={begin}>
        <label htmlFor="name">Full name</label>
        <input id="name" value={replacement ? existing?.full_name ?? '' : name} required maxLength={100}
          autoComplete="name" readOnly={!!replacement} onChange={e => setName(e.target.value)} />
        <label htmlFor="email">Email</label>
        <input id="email" type="email" value={replacement ? existing?.email ?? '' : email} required maxLength={254}
          autoComplete="email" readOnly={!!replacement} onChange={e => setEmail(e.target.value)} />
        <label className="checkbox"><input type="checkbox" required checked={consent} onChange={e => setConsent(e.target.checked)} />
          I consent to storing my face template on this computer for this demo.</label>
        <button className="button" disabled={busy || (!!replacement && !existing)}>Continue to enrollment</button>
      </form>
      {error && <p className="error" role="alert">{error}</p>}
    </section> : <div className="work-grid">
      <CameraPanel camera={camera} box={sample?.box} phoneEnabled={phoneEnabled} locked={busy} />
      <section className="panel panel-body stack">
        <h2>Capture face samples</h2>
        <p className="feedback" role="status">{ready ? 'All samples captured. Save the profile.' : camera.status === 'live'
          ? guidanceFor(sample?.quality ?? '') : 'Start a camera to begin capture.'}</p>
        <label htmlFor="samples">{sample?.count ?? 0}/{session.required} samples</label>
        <progress id="samples" value={sample?.count ?? 0} max={session.required} />
        <p className="muted">Look toward the camera and move your head slightly between samples.</p>
        {error && <p className="error" role="alert">{error}</p>}
        <button className="button" disabled={!ready || busy} onClick={complete}>Save profile</button>
        <button className="button subtle" disabled={busy} onClick={cancel}>Cancel enrollment</button>
      </section>
    </div>}
  </>
}
