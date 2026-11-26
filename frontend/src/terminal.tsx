import { CheckCircle2, Maximize2, Minimize2, ScanFace } from 'lucide-react'
import { useEffect, useState } from 'react'
import { formatTime, guidanceFor, socket } from './api'
import type { Box, Pair, Receipt, Settings } from './api'
import { CameraPanel, type Tone, useCamera } from './camera'

type LiveFace = {
  box: Box | null
  state: string
  full_name?: string
  public_id?: string
  stable_count?: number
  checked_in_at?: string
  detection_confidence: number | null
  similarity: number | null
}

type Result = {
  state: 'started' | 'frame' | 'error'
  seq?: number
  detail?: string
  quality: string
  faces: LiveFace[]
  records: Receipt[]
  threshold: number
  stable_frames: number
  latency_ms: number
}

export function useLiveAttendance(live: boolean, pair: Pair | null, frame: () => Promise<Blob | null>) {
  const [result, setResult] = useState<Result | null>(null)
  const [records, setRecords] = useState<Receipt[]>([])
  const [error, setError] = useState('')

  useEffect(() => {
    setResult(null)
    setError('')
    if (!live) return

    const ws = socket('/ws/attendance')
    let counter = 0
    let pending = false
    let capturing = false
    let announced = false
    let stopped = false
    let lastMessage = performance.now()
    let lastSent = -Infinity
    let stale = false

    ws.onopen = () => ws.send(JSON.stringify(pair ? {
      phone_pair: pair.pair_id, viewer_key: pair.viewer_key,
    } : {}))

    function fail(message: string) {
      if (stopped) return
      stopped = true
      setError(message)
      setResult(null)
      ws.close()
    }

    ws.onmessage = event => {
      if (stopped) return
      const data: Result = JSON.parse(event.data)
      if (data.state === 'error') return fail(data.detail ?? 'Recognition failed.')
      if (data.state === 'started') {
        announced = true
        lastMessage = performance.now()
        return
      }
      if (data.seq !== counter) return
      pending = false
      stale = false
      lastMessage = performance.now()
      setResult(data)
      if (data.records.length) setRecords(previous => {
        const arriving = new Set(data.records.map(keyFor))
        return [...data.records, ...previous.filter(record => !arriving.has(keyFor(record)))].slice(0, 8)
      })
    }
    ws.onclose = () => fail('Recognition disconnected. Start the camera again.')

    const timer = window.setInterval(async () => {
      const now = performance.now()
      if (stopped) return
      if (now - lastMessage > 8000) return fail('No camera frames received. Check your camera and try again.')
      if (now - lastMessage > 1850 && !stale) {
        stale = true
        setResult(null)
      }
      if (!announced || pending || capturing || now - lastSent < 245 || ws.readyState !== WebSocket.OPEN) return
      capturing = true
      try {
        const blob = await frame()
        if (!blob || stopped || ws.readyState !== WebSocket.OPEN) return
        const header = new ArrayBuffer(4)
        new DataView(header).setUint32(0, ++counter)
        ws.send(new Blob([header, blob]))
        pending = true
        lastSent = now
      } catch {
        fail('Could not send the camera frame.')
      } finally {
        capturing = false
      }
    }, 40)
    const close = () => {
      stopped = true
      window.clearInterval(timer)
      ws.close()
    }
    window.addEventListener('pagehide', close)
    return () => {
      window.removeEventListener('pagehide', close)
      close()
    }
  }, [live, pair, frame])

  return { result, records, error }
}

function keyFor(record: Receipt) {
  return record.public_id + record.attendance_date
}

function faceLabel(face: LiveFace) {
  if (face.full_name) return face.full_name
  if (face.state === 'unknown') return 'Unknown face'
  if (face.state === 'ambiguous') return 'Uncertain match'
  return 'Adjust position'
}

function faceTone(face: LiveFace): Tone | undefined {
  if (face.state === 'checked_in' || face.state === 'already_checked_in') return 'ok'
  if (face.state === 'matching') return 'pending'
  if (face.state === 'unknown' || face.state === 'ambiguous') return 'bad'
  return undefined
}

// Kiosk mode hides the sidebar and asks for fullscreen. Leaving fullscreen with Esc ends it.
function useKiosk() {
  const [kiosk, setKiosk] = useState(false)
  useEffect(() => {
    document.body.classList.toggle('kiosk', kiosk)
    if (!kiosk) return
    const exit = () => { if (!document.fullscreenElement) setKiosk(false) }
    document.addEventListener('fullscreenchange', exit)
    return () => {
      document.removeEventListener('fullscreenchange', exit)
      document.body.classList.remove('kiosk')
    }
  }, [kiosk])
  async function toggle() {
    if (kiosk) {
      setKiosk(false)
      if (document.fullscreenElement) await document.exitFullscreen().catch(() => {})
      return
    }
    setKiosk(true)
    await document.documentElement.requestFullscreen?.().catch(() => {})
  }
  return { kiosk, toggle }
}

function faceStatus(face: LiveFace, required: number) {
  switch (face.state) {
    case 'checked_in': return 'Checked in'
    case 'already_checked_in': return 'Already checked in today'
    case 'matching': return `Recognizing · ${face.stable_count}/${required} frames`
    case 'unknown': return 'Face not recognized. No check-in recorded.'
    case 'ambiguous': return 'Uncertain match. Step closer and try again.'
    default: return guidanceFor(face.state)
  }
}

export function Terminal({ settings }: { settings: Settings }) {
  const camera = useCamera()
  const [error, setError] = useState('')
  const attendance = useLiveAttendance(camera.status === 'live', camera.pair, camera.frame)
  const { result, records } = attendance
  const { kiosk, toggle } = useKiosk()
  const [flash, setFlash] = useState(false)
  const stopCamera = camera.stop
  const latest = records[0]
  useEffect(() => {
    if (!latest?.created) return
    setFlash(true)
    const timer = window.setTimeout(() => setFlash(false), 1200)
    return () => window.clearTimeout(timer)
  }, [latest])
  useEffect(() => {
    if (attendance.error && camera.status === 'live') {
      stopCamera()
      setError(attendance.error)
    } else if (camera.status === 'live' || camera.status === 'starting') {
      setError('')
    }
  }, [attendance.error, camera.status, stopCamera])

  return <>
    <div className="page-heading page-actions">
      <div>
        <h1>Attendance terminal</h1>
        <p>Start a camera. Enrolled faces are recognized and checked in automatically.</p>
      </div>
      <button className="button subtle" onClick={toggle} aria-pressed={kiosk}>
        {kiosk ? <><Minimize2 size={16} aria-hidden="true" />Exit kiosk</> : <><Maximize2 size={16} aria-hidden="true" />Kiosk mode</>}
      </button>
    </div>
    <div className="work-grid">
      <CameraPanel camera={camera} phoneEnabled={settings.phone_enabled}
        faces={result?.faces.map(face => ({ box: face.box, label: faceLabel(face), tone: faceTone(face) }))} />
      <section className={flash ? 'panel panel-body stack check-in-flash' : 'panel panel-body stack'}>
        <div className="profile-summary"><ScanFace size={24} aria-hidden="true" /><h2>Live recognition</h2></div>
        {!result?.faces.length && <p className="feedback" aria-live="polite">
          {camera.status !== 'live' ? 'Choose a camera to begin.' :
            result?.quality === 'too_many_faces' ? 'Too many faces in the frame. Check in in smaller groups.' :
              guidanceFor(result?.quality ?? '')}
        </p>}
        {error && <p className="error" role="alert">{error}</p>}
        <dl className="scores">
          <Metric label="Camera FPS" value={camera.fps?.toFixed(1) ?? '—'} />
          <Metric label="Inference time" value={result ? result.latency_ms + ' ms' : '—'} />
          <Metric label="Minimum similarity" value={(result?.threshold ?? settings.threshold).toFixed(3)} />
        </dl>
        {result?.faces.map((face, index) => <div className={'live-face tone-' + (faceTone(face) ?? 'none')} key={face.public_id ?? index}>
          <strong>{faceLabel(face)}</strong>
          {face.public_id && <small>{face.public_id}</small>}
          <p className="feedback">{faceStatus(face, result.stable_frames)}</p>
          {face.checked_in_at && <time dateTime={face.checked_in_at}>{formatTime(face.checked_in_at, settings.timezone)}</time>}
          <dl className="scores">
            <Metric label="Detection confidence" value={face.detection_confidence != null ? (face.detection_confidence * 100).toFixed(1) + '%' : '—'} />
            <Metric label="Cosine similarity" value={face.similarity?.toFixed(3) ?? '—'} />
          </dl>
        </div>)}
      </section>
    </div>
    {records.length > 0 && <section className="panel panel-body terminal-records" aria-live="polite">
      <h2>This session</h2>
      <ul className="events">{records.map(record => <li key={keyFor(record)}>
        <div className="profile-summary"><CheckCircle2 size={18} aria-hidden="true" /><div>
          <strong>{record.full_name}</strong>
          <small>{record.created ? 'Check-in saved' : 'Already checked in today'} · {record.public_id}</small>
        </div></div>
        <time dateTime={record.checked_in_at}>{formatTime(record.checked_in_at, settings.timezone)}</time>
      </li>)}</ul>
    </section>}
  </>
}

function Metric({ label, value }: { label: string; value: string }) {
  return <div><dt>{label}</dt><dd>{value}</dd></div>
}
