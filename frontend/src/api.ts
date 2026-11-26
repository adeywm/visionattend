export type Profile = {
  public_id: string
  full_name: string
  email: string
  created_at: string
  sample_count: number
}

export type RecordRow = {
  public_id: string
  full_name: string
  attendance_date: string
  checked_in_at: string
  similarity: number
}

export type Receipt = RecordRow & { created: boolean }
export type Page<T> = { items: T[]; total: number }
export type Box = [number, number, number, number]
export type Settings = {
  models_ready: boolean
  phone_enabled: boolean
  timezone: string
  threshold: number
  stable_frames: number
}
export type Pair = { url: string; pair_id: string; viewer_key: string; expires_in: number }

export async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch('/api' + path, options)
  if (!response.ok) {
    const data = await response.json().catch(() => ({}))
    const detail = data.detail
    throw new Error(typeof detail === 'string' ? detail :
      Array.isArray(detail) && detail[0]?.msg ? detail[0].msg : 'The request failed. Try again.')
  }
  if (response.status === 204) return undefined as T
  return response.json()
}

export function json(method: string, body: unknown): RequestInit {
  return { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }
}

export function socket(path: string) {
  const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:'
  return new WebSocket(scheme + '//' + location.host + path)
}

export function errorText(error: unknown) {
  return error instanceof Error ? error.message : 'Something went wrong. Try again.'
}

export function formatTime(iso: string, zone?: string, showDate = false) {
  return new Intl.DateTimeFormat(undefined, {
    timeZone: zone, ...(showDate && { dateStyle: 'medium' }), timeStyle: 'short',
  }).format(new Date(iso))
}

const guidance: Record<string, string> = {
  usable: 'Face in frame. Hold steady.',
  no_face: 'Center your face in the frame.',
  multiple_faces: 'Only one person should be in the frame.',
  face_too_small: 'Move closer to the camera.',
  blurry: 'Hold still and find brighter lighting.',
  change_pose: 'Turn your head slightly before the next sample.',
  different_face: 'Use the same person for all samples.',
  invalid_frame: 'Could not read the frame. Check the camera.',
  inference_error: 'Could not analyze the frame. Try again.',
  models_unavailable: 'Download both face models and restart VisionAttend.',
  mismatch: 'Face did not match this profile. Check the ID and try again.',
  complete: 'All samples captured. Save the profile.',
}
export const guidanceFor = (quality: string) => guidance[quality] ?? 'Waiting for a camera frame.'
