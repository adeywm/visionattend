// @vitest-environment jsdom

import { act, cleanup, fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useCamera } from './camera'
import { PhoneCamera } from './phone'
import { Register } from './register'
import { useLiveAttendance } from './terminal'

class MockSocket {
  static OPEN = 1
  static last: MockSocket
  readyState = 1
  onopen?: () => void
  onmessage?: (event: { data: string }) => void
  close = vi.fn()
  send = vi.fn()

  constructor() {
    MockSocket.last = this
  }

  message(data: unknown) {
    this.onmessage?.({ data: JSON.stringify(data) })
  }
}

beforeEach(() => {
  vi.stubGlobal('WebSocket', MockSocket)
  localStorage.clear()
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('capture and verification', () => {
  it('keeps the camera off until started and stops its tracks on navigation', async () => {
    const stop = vi.fn()
    const permission = vi.fn(async () => ({
      getTracks: () => [track], getVideoTracks: () => [track],
    }))
    const track = { onended: null, stop, getSettings: () => ({ facingMode: 'user' }) }
    Object.defineProperty(navigator, 'mediaDevices', {
      configurable: true, value: { getUserMedia: permission },
    })
    vi.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue()
    function CameraFixture() {
      const camera = useCamera()
      return <><video ref={camera.video} /><button onClick={camera.startLocal}>Start the camera</button><p>{camera.status}</p></>
    }
    const view = render(<CameraFixture />)
    expect(permission).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Start the camera' }))
    await screen.findByText('live')
    view.unmount()
    expect(stop).toHaveBeenCalledOnce()
  })

  it('shows an actionable camera permission error', async () => {
    Object.defineProperty(navigator, 'mediaDevices', {
      configurable: true,
      value: { getUserMedia: vi.fn().mockRejectedValue(new DOMException('denied', 'NotAllowedError')) },
    })
    function CameraFixture() {
      const camera = useCamera()
      return <><video ref={camera.video} /><button onClick={camera.startLocal}>Start the camera</button><p>{camera.error}</p></>
    }
    render(<CameraFixture />)
    fireEvent.click(screen.getByRole('button', { name: 'Start the camera' }))
    await screen.findByText(/camera permission denied/i)
  })

  it('shows automatic check-ins and ignores stale frame results', async () => {
    const capture = vi.fn().mockResolvedValue(new Blob(['frame']))
    const view = renderHook(({ live }) => useLiveAttendance(live, null, capture), {
      initialProps: { live: true },
    })
    const ws = MockSocket.last
    act(() => {
      ws.onopen?.()
      ws.message({ state: 'started' })
    })
    await waitFor(() => expect(ws.send).toHaveBeenCalledTimes(2))
    const receipt = { public_id: 'VA-000001', full_name: 'Demo', attendance_date: '2026-11-26',
      checked_in_at: '2026-11-26T12:30:00Z', similarity: 0.87, created: true }
    act(() => ws.message({ state: 'frame', seq: 91, records: [receipt] }))
    expect(view.result.current.records).toHaveLength(0)
    act(() => ws.message({ state: 'frame', seq: 1, records: [receipt] }))
    expect(view.result.current.records).toEqual([receipt])
    view.rerender({ live: false })
    expect(ws.close).toHaveBeenCalledOnce()
    expect(view.result.current.result).toBeNull()
  })

  it('moves from details to enrollment and cancels without creating a profile', async () => {
    const request = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ session_id: 'enroll', required: 5, expires_in: 300 }), { status: 201 }))
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
    vi.stubGlobal('fetch', request)
    render(<MemoryRouter><Register phoneEnabled /></MemoryRouter>)
    fireEvent.change(screen.getByLabelText('Full name'), { target: { value: 'Demo Operator' } })
    fireEvent.change(screen.getByLabelText('Email'), { target: { value: 'demo@example.com' } })
    fireEvent.click(screen.getByRole('checkbox'))
    fireEvent.click(screen.getByRole('button', { name: 'Continue to enrollment' }))
    await screen.findByText('Capture face samples')
    expect(screen.getByRole<HTMLButtonElement>('button', { name: 'Save profile' }).disabled).toBe(true)
    expect(screen.getByRole('button', { name: 'Pair phone' })).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel enrollment' }))
    await screen.findByText('Profile details')
    expect(request).toHaveBeenCalledWith('/api/enrollment/enroll', expect.objectContaining({ method: 'DELETE' }))
    expect(request).toHaveBeenCalledTimes(2)
  })

  it('accepts a fresh QR link in an existing phone tab after stopping', async () => {
    const track = { onended: null, stop: vi.fn(), getSettings: () => ({ facingMode: 'user' }) }
    const permission = vi.fn(async () => ({ getTracks: () => [track], getVideoTracks: () => [track] }))
    Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: { getUserMedia: permission } })
    vi.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue()
    history.replaceState(null, '', '/phone#first.key')
    try {
      render(<PhoneCamera />)
      fireEvent.click(screen.getByRole('button', { name: 'Start camera' }))
      await waitFor(() => expect(MockSocket.last?.onopen).toBeDefined())
      const first = MockSocket.last
      fireEvent.click(screen.getByRole('button', { name: 'Stop camera' }))
      expect(track.stop).toHaveBeenCalledOnce()
      act(() => {
        history.replaceState(null, '', '/phone#next.fresh')
        window.dispatchEvent(new HashChangeEvent('hashchange'))
      })
      fireEvent.click(screen.getByRole('button', { name: 'Start camera' }))
      await waitFor(() => expect(MockSocket.last).not.toBe(first))
      MockSocket.last.onopen?.()
      expect(MockSocket.last.send).toHaveBeenCalledWith(JSON.stringify({ pair_id: 'next', key: 'fresh' }))
    } finally {
      history.replaceState(null, '', '/')
    }
  })
})
