import { Camera, ScanFace, Square } from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { socket } from './api'
import { cameraError, jpeg, observeVideo, startStream } from './camera'

export function PhoneCamera() {
  const video = useRef<HTMLVideoElement>(null)
  const stream = useRef<MediaStream | null>(null)
  const connection = useRef<WebSocket | null>(null)
  const captureTimer = useRef<number | undefined>(undefined)
  const stopFps = useRef<(() => void) | null>(null)
  const epoch = useRef(0)
  const [link, setLink] = useState(() => location.hash.slice(1).split('.'))
  const [facing, setFacing] = useState('user')
  const [state, setState] = useState<'off' | 'starting' | 'live' | 'ended'>('off')
  const [error, setError] = useState('')

  const release = useCallback(() => {
    epoch.current += 1
    window.clearInterval(captureTimer.current)
    stopFps.current?.()
    stopFps.current = null
    if (connection.current) connection.current.close()
    connection.current = null
    stream.current?.getTracks().forEach(track => {
      track.onended = null
      track.stop()
    })
    stream.current = null
    if (video.current) video.current.srcObject = null
  }, [])

  useEffect(() => {
    function resetPairing() {
      if (!location.hash) return
      release()
      setLink(location.hash.slice(1).split('.'))
      setError('')
      setState('off')
    }
    window.addEventListener('hashchange', resetPairing)
    window.addEventListener('pagehide', release)
    return () => {
      window.removeEventListener('hashchange', resetPairing)
      window.removeEventListener('pagehide', release)
      release()
    }
  }, [release])

  async function start() {
    if (link.length !== 2 || !link[0] || !link[1] || !video.current) {
      return setError('Scan the pairing code shown on the laptop.')
    }
    setError('')
    setState('starting')
    const current = ++epoch.current
    const element = video.current
    try {
      const media = await startStream({
        width: { ideal: 640 }, height: { ideal: 480 }, facingMode: { ideal: facing },
      })
      if (current !== epoch.current) {
        media.getTracks().forEach(track => track.stop())
        return
      }
      stream.current = media
      element.srcObject = media
      await element.play()
      if (current !== epoch.current) return
      const mirrored = (media.getVideoTracks()[0].getSettings().facingMode ?? facing) !== 'environment'
      element.classList.toggle('mirror', mirrored)
      const ws = connection.current = socket('/ws/camera')
      let fps = 0
      let sending = false
      const canvas = document.createElement('canvas')
      stopFps.current = observeVideo(element, number => { fps = Math.min(number, 240) })
      media.getVideoTracks()[0].onended = () => {
        release()
        setState('ended')
        setError('Camera disconnected. Scan a fresh code to reconnect.')
      }
      ws.onopen = () => ws.send(JSON.stringify({ pair_id: link[0], key: link[1] }))
      ws.onmessage = event => {
        if (current !== epoch.current || JSON.parse(event.data).status !== 'connected') return
        setState('live')
        history.replaceState(null, '', location.pathname)
        captureTimer.current = window.setInterval(async () => {
          if (sending || ws.readyState !== WebSocket.OPEN || ws.bufferedAmount > 0) return
          sending = true
          try {
            const frame = await jpeg(element, canvas)
            if (!frame || current !== epoch.current || ws.readyState !== WebSocket.OPEN) return
            const header = new DataView(new ArrayBuffer(5))
            header.setFloat32(0, fps)
            header.setUint8(4, Number(mirrored))
            ws.send(new Blob([header.buffer, frame]))
          } catch {
            release()
            setState('ended')
            setError('Could not send camera frames. Scan a fresh code to reconnect.')
          } finally {
            sending = false
          }
        }, 100)
      }
      ws.onclose = () => {
        if (current !== epoch.current) return
        release()
        setState('ended')
      }
    } catch (problem) {
      if (current === epoch.current) {
        release()
        setError(cameraError(problem))
        setState('off')
      }
    }
  }

  return <main className="phone-page">
    <div className="brand"><ScanFace size={24} aria-hidden="true" />VisionAttend</div>
    <h1>Phone camera</h1>
    <p>Send camera frames to the laptop on your Wi-Fi network.</p>
    <video ref={video} autoPlay muted playsInline hidden={state !== 'live'} aria-label="Phone camera preview" />
    {error && <p className="error" role="alert">{error}</p>}
    <p className="feedback" role="status">
      {state === 'live' ? 'Camera live. Continue on the laptop.' :
        state === 'ended' ? 'Camera off. Scan a new code to reconnect.' :
        state === 'starting' ? 'Connecting to the laptop…' : 'Choose a camera to start.'}
    </p>
    {state === 'off' && <div className="stack">
      <label htmlFor="facing">Camera</label>
      <select id="facing" value={facing} onChange={event => setFacing(event.target.value)}>
        <option value="user">Front camera</option><option value="environment">Rear camera</option>
      </select>
      <button className="button" onClick={start}><Camera size={17} />Start camera</button>
    </div>}
    {(state === 'live' || state === 'starting') && <button className="button subtle" onClick={() => {
      release()
      setState('ended')
    }}><Square size={16} />Stop camera</button>}
  </main>
}
