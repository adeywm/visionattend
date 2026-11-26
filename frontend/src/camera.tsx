import { Camera, Globe, Smartphone, Square, VideoOff } from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { toDataURL } from 'qrcode'
import { api, errorText, type Box, type Pair, socket } from './api'

const DEVICE_KEY = 'visionattend.camera'
const MAX_FRAME = 512 * 1024
export const preferredCamera = () => localStorage.getItem(DEVICE_KEY) ?? ''
export const saveCamera = (device: string) => localStorage.setItem(DEVICE_KEY, device)
export const devices = async () => (await navigator.mediaDevices.enumerateDevices()).filter(item => item.kind === 'videoinput')

export function cameraError(error: unknown) {
  const name = error instanceof DOMException ? error.name : ''
  if (name === 'NotAllowedError') return 'Camera permission denied. Allow it in your browser and try again.'
  if (name === 'NotFoundError' || name === 'OverconstrainedError') return 'Camera unavailable. Select another camera or pair your phone.'
  if (name === 'NotReadableError') return 'Camera is busy. Close other apps using it and try again.'
  return errorText(error)
}

export async function startStream(options: MediaTrackConstraints) {
  if (!navigator.mediaDevices?.getUserMedia) throw new Error('Camera access requires localhost or trusted HTTPS.')
  const stream = await navigator.mediaDevices.getUserMedia({ audio: false, video: options })
  return stream
}

export function observeVideo(video: HTMLVideoElement, update: (fps: number) => void) {
  if (!video.requestVideoFrameCallback) return () => {}
  let frameId = 0
  let previous: { time: number; count: number } | undefined
  function observe(now: number, frame: VideoFrameCallbackMetadata) {
    if (!previous) previous = { time: now, count: frame.presentedFrames }
    else if (now - previous.time >= 900) {
      update((frame.presentedFrames - previous.count) * 1000 / (now - previous.time))
      previous = { time: now, count: frame.presentedFrames }
    }
    frameId = video.requestVideoFrameCallback(observe)
  }
  frameId = video.requestVideoFrameCallback(observe)
  return () => video.cancelVideoFrameCallback(frameId)
}

export async function jpeg(video: HTMLVideoElement, canvas: HTMLCanvasElement) {
  if (video.readyState < 2 || !video.videoWidth || !video.videoHeight) return null
  const scale = Math.min(1, 640 / Math.max(video.videoWidth, video.videoHeight))
  canvas.width = Math.round(video.videoWidth * scale)
  canvas.height = Math.round(video.videoHeight * scale)
  canvas.getContext('2d')?.drawImage(video, 0, 0, canvas.width, canvas.height)
  return new Promise<Blob | null>(resolve => canvas.toBlob(
    blob => resolve(blob && blob.size <= MAX_FRAME ? blob : null), 'image/jpeg', 0.78,
  ))
}

type SourceState = 'off' | 'starting' | 'pairing' | 'live' | 'error'
const constraints = { width: { ideal: 640 }, height: { ideal: 480 } }

export function useCamera() {
  const video = useRef<HTMLVideoElement>(null)
  const canvas = useRef<HTMLCanvasElement | null>(null)
  const stream = useRef<MediaStream | null>(null)
  const viewer = useRef<WebSocket | null>(null)
  const releaseFps = useRef<(() => void) | null>(null)
  const epoch = useRef(0)
  const remote = useRef<Blob | null>(null)
  const pictureUrl = useRef('')
  const lastFrame = useRef<Blob | number | null>(null)
  const [status, setStatus] = useState<SourceState>('off')
  const [error, setError] = useState('')
  const [fps, setFps] = useState<number | null>(null)
  const [aspect, setAspect] = useState({ width: 4, height: 3 })
  const [mirror, setMirror] = useState(true)
  const [picture, setPicture] = useState('')
  const [pair, setPair] = useState<Pair | null>(null)
  const [qr, setQr] = useState('')

  const release = useCallback(() => {
    epoch.current += 1
    releaseFps.current?.()
    releaseFps.current = null
    for (const track of stream.current?.getTracks() ?? []) {
      track.onended = null
      track.stop()
    }
    stream.current = null
    if (viewer.current) viewer.current.close()
    viewer.current = null
    if (video.current) video.current.srcObject = null
    remote.current = lastFrame.current = null
    if (pictureUrl.current) URL.revokeObjectURL(pictureUrl.current)
    pictureUrl.current = ''
  }, [])

  const stop = useCallback(() => {
    release()
    setStatus('off')
    setError('')
    setFps(null)
    setPair(null)
    setQr('')
    setPicture('')
  }, [release])

  useEffect(() => {
    window.addEventListener('pagehide', stop)
    return () => {
      window.removeEventListener('pagehide', stop)
      release()
    }
  }, [release, stop])

  const fail = useCallback((message: string) => {
    stop()
    setError(message)
    setStatus('error')
  }, [stop])

  async function startLocal() {
    stop()
    setStatus('starting')
    const current = epoch.current
    const element = video.current
    if (!element) return fail('Camera preview is unavailable.')
    const preferred = preferredCamera()
    try {
      const active = await startStream({
        ...constraints, ...(preferred ? { deviceId: { exact: preferred } } : { facingMode: { ideal: 'user' } }),
      })
      if (current !== epoch.current) {
        active.getTracks().forEach(track => track.stop())
        return
      }
      stream.current = active
      element.srcObject = active
      active.getVideoTracks()[0].onended = () => fail('Camera disconnected. Choose a camera and start again.')
      await element.play()
      if (current !== epoch.current) return
      setMirror(active.getVideoTracks()[0].getSettings().facingMode !== 'environment')
      setAspect({ width: element.videoWidth || 4, height: element.videoHeight || 3 })
      releaseFps.current = observeVideo(element, setFps)
      setStatus('live')
    } catch (problem) {
      if (current === epoch.current) fail(cameraError(problem))
    }
  }

  async function startPhone() {
    stop()
    setStatus('starting')
    const current = epoch.current
    try {
      const created = await api<Pair>('/phone/pairs', { method: 'POST' })
      if (epoch.current !== current) return
      const image = await toDataURL(created.url, { width: 184, margin: 2 })
      if (epoch.current !== current) return
      setPair(created)
      setQr(image)
      setStatus('pairing')
      const ws = socket('/ws/phone/view')
      ws.binaryType = 'arraybuffer'
      viewer.current = ws
      ws.onopen = () => ws.send(JSON.stringify({ pair_id: created.pair_id, key: created.viewer_key }))
      ws.onmessage = event => receiveRemote(event, current)
      ws.onclose = () => {
        if (epoch.current === current) fail('Phone camera disconnected or the pairing link expired. Pair again or use a local camera.')
      }
    } catch (problem) {
      if (epoch.current === current) fail(errorText(problem))
    }
  }

  function receiveRemote(event: MessageEvent, current: number) {
    if (epoch.current !== current) return
    if (typeof event.data === 'string') {
      const status = JSON.parse(event.data)
      if (status.status === 'error') fail(status.detail)
      return
    }
    const data: ArrayBuffer = event.data
    if (data.byteLength <= 5) return
    const meta = new DataView(data)
    setFps(meta.getFloat32(0) || null)
    setMirror(Boolean(meta.getUint8(4)))
    const blob = new Blob([data.slice(5)], { type: 'image/jpeg' })
    remote.current = blob
    const url = URL.createObjectURL(blob)
    if (pictureUrl.current) URL.revokeObjectURL(pictureUrl.current)
    pictureUrl.current = url
    setPicture(url)
    setStatus('live')
  }

  function startIp(url: string) {
    stop()
    setStatus('starting')
    const current = epoch.current
    try {
      const ws = socket('/ws/ip-camera')
      ws.binaryType = 'arraybuffer'
      viewer.current = ws
      ws.onopen = () => {
        ws.send(JSON.stringify({ url }))
        ws.onopen = null
      }
      ws.onmessage = event => receiveRemote(event, current)
      ws.onclose = () => {
        if (epoch.current === current) fail('IP camera disconnected. Check the stream and reconnect.')
      }
    } catch (problem) {
      if (epoch.current === current) fail(errorText(problem))
    }
  }

  const frame = useCallback(async () => {
    if (remote.current) {
      if (lastFrame.current === remote.current) return null
      lastFrame.current = remote.current
      return remote.current
    }
    const element = video.current
    if (!stream.current || !element || lastFrame.current === element.currentTime) return null
    const current = epoch.current
    lastFrame.current = element.currentTime
    canvas.current ??= document.createElement('canvas')
    const blob = await jpeg(element, canvas.current)
    return current === epoch.current ? blob : null
  }, [])

  return { video, status, stop, startLocal, startPhone, startIp, frame, pair, qr, picture, fps, aspect, setAspect, mirror, error }
}

export type CameraState = ReturnType<typeof useCamera>

export type Tone = 'ok' | 'pending' | 'bad'
export type Overlay = { box: Box | null; label: string; tone?: Tone }

export function CameraPanel({ camera, box, faces, phoneEnabled, locked = false }: {
  camera: CameraState; box?: Box | null; faces?: Overlay[]; phoneEnabled: boolean; locked?: boolean
}) {
  const [copiedPair, setCopiedPair] = useState<{ id: string; message: string } | null>(null)
  const [showIp, setShowIp] = useState(false)
  const [ipUrl, setIpUrl] = useState('')
  const [showUrl, setShowUrl] = useState(false)
  const live = camera.status === 'live'
  const busy = camera.status === 'pairing' || camera.status === 'starting'
  const overlays: Overlay[] = faces ?? (box ? [{ box, label: '' }] : [])
  return <section className="panel camera-panel" aria-label="Camera">
    <div className="panel-heading">
      <h2>Camera</h2>
      <span className={live ? 'status active' : 'status'}>{live ? 'Live' : busy ? 'Connecting' : 'Off'}</span>
    </div>
    <div className="viewport">
      <div className="camera-content" style={{
        aspectRatio: `${camera.aspect.width}/${camera.aspect.height}`,
        width: `min(100%, ${410 * camera.aspect.width / camera.aspect.height}px)`,
      }}>
        <video aria-label="Local camera preview" ref={camera.video} autoPlay muted playsInline
          hidden={!live || !!camera.picture} className={camera.mirror ? 'mirror' : ''}
          onResize={event => {
            const { videoWidth: width, videoHeight: height } = event.currentTarget
            if (width && height) camera.setAspect({ width, height })
          }} />
        {camera.picture && <img src={camera.picture} alt="Live network camera preview" className={camera.mirror ? 'mirror' : ''}
          onLoad={event => {
            const { naturalWidth: width, naturalHeight: height } = event.currentTarget
            if (width !== camera.aspect.width || height !== camera.aspect.height) camera.setAspect({ width, height })
          }} />}
        {live && overlays.map(({ box: previewBox, label, tone }, index) => previewBox && <div key={index}
          className={tone ? 'face-box tone-' + tone : 'face-box'} aria-hidden="true" style={{
          left: `${(camera.mirror ? 1 - previewBox[0] - previewBox[2] : previewBox[0]) * 100}%`,
          top: `${previewBox[1] * 100}%`, width: `${previewBox[2] * 100}%`, height: `${previewBox[3] * 100}%`,
        }}>{label && <span>{label}</span>}</div>)}
      </div>
      {!live && <div className="camera-empty">
        {camera.status === 'pairing' && camera.pair ? <>
          <img src={camera.qr} width="160" height="160" alt="QR code to pair the phone camera" />
          <p>Scan with your phone on the same Wi-Fi.</p>
          <button className="button subtle" onClick={async () => {
            if (!camera.pair) return
            const { pair_id, url } = camera.pair
            try {
              await navigator.clipboard.writeText(url)
              setCopiedPair({ id: pair_id, message: 'Link copied' })
            } catch {
              setCopiedPair({ id: pair_id, message: 'Copy failed. Scan QR code.' })
            }
          }}>Copy pairing link</button>
          {copiedPair?.id === camera.pair.pair_id && <p role="status">{copiedPair.message}</p>}
        </> : <><VideoOff size={28} aria-hidden="true" /><p>{busy ? 'Connecting camera…' : 'Camera preview'}</p></>}
      </div>}
    </div>
    <div className="panel-body camera-actions">
      {live || busy ? <button className="button subtle" onClick={camera.stop} disabled={locked}><Square size={16} />Stop camera</button> :
        <>
          <button className="button" onClick={() => { setShowIp(false); camera.startLocal() }} disabled={locked}><Camera size={17} />Start camera</button>
          <button className="button subtle" onClick={() => setShowIp(!showIp)} aria-expanded={showIp} disabled={locked}><Globe size={17} />IP camera</button>
          {phoneEnabled ? <button className="button subtle" onClick={() => { setShowIp(false); camera.startPhone() }} disabled={locked}><Smartphone size={17} />Pair phone</button> :
            <Link className="button subtle" to="/settings?phone=setup"><Smartphone size={17} />Pair phone</Link>}
        </>}
    </div>
    {showIp && !live && !busy && <form className="panel-body stack ip-camera-form" onSubmit={event => {
      event.preventDefault()
      // Keep the URL in memory only, so a dropped stream can reconnect without retyping it.
      camera.startIp(ipUrl.trim())
      setShowUrl(false)
    }}>
      <label htmlFor="ip-stream">Camera stream URL</label>
      <input id="ip-stream" type={showUrl ? 'text' : 'password'} value={ipUrl} required autoComplete="off" spellCheck={false}
        maxLength={2048} placeholder="http://192.168.1.20:8080/video" onChange={event => setIpUrl(event.target.value)} />
      <label className="checkbox"><input type="checkbox" checked={showUrl} onChange={event => setShowUrl(event.target.checked)} />Show URL</label>
      <p className="muted">Use the stream URL from your camera or phone app, on your private network. Supported: RTSP, such as <code>rtsp://192.168.1.20:554/live</code>, and HTTP MJPEG, such as <code>http://192.168.1.20:8080/video</code> in IP Webcam. HTTPS works only with a certificate this computer trusts. Use <code>http://</code> for self-signed phone apps.</p>
      <button className="button" disabled={locked}>Connect IP camera</button>
    </form>}
    {camera.error && <p className="error camera-error" role="alert">{camera.error}</p>}
  </section>
}
