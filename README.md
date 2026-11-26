# VisionAttend

VisionAttend is a local face-recognition attendance system. Register a consenting person once with five face samples. Then point any camera at the entrance: a laptop webcam, a phone, or an RTSP/MJPEG IP camera. Every enrolled face in view is recognized, and the first confident match each day is saved with a server timestamp.

Everything runs on one computer. Models run on the CPU, data stays in a local SQLite file, and raw camera frames are never stored.

> **Demo scope.** There is no login, no liveness detection, and no encryption at rest. A printed photo can fool it. Use it only with consenting adults and demo data.

## Features

- **Multi-face recognition.** Recognizes up to 8 people in one frame, each with their own matching state.
- **Stable, high-confidence check-ins.** A person is saved only after several consecutive frames match the same profile above the similarity threshold, with a clear lead over the next-best profile.
- **One record per person per day.** A database constraint enforces this. Later sightings show "already checked in" with the original time.
- **Any camera source:**
  - Laptop webcam.
  - IP camera or phone camera app (RTSP or HTTP MJPEG).
  - A phone's browser camera paired over Wi-Fi by QR code.
- **Live terminal.** Face boxes are colored by state: green when checked in, amber while matching, red when unknown or uncertain. The panel flashes on each new check-in, and kiosk mode goes fullscreen for an entrance screen.
- **Dashboard, users, records.** Shows a 7-day chart, lets you search profiles, re-enroll or delete a person, filter records, and export CSV.

## How it works

```mermaid
flowchart LR
  subgraph Sources
    W[Laptop webcam]
    P[Phone browser\nQR pairing over HTTPS]
    I[IP camera / phone app\nRTSP or MJPEG]
  end
  W --> UI[React terminal]
  P -->|LAN relay| UI
  I -->|isolated worker process| UI
  UI -->|JPEG frames over WebSocket| API[FastAPI]
  API --> D[YuNet\nface detection]
  D --> R[SFace\nalign + 128-d embedding]
  R --> M[Cosine match\nagainst templates]
  M --> S{Stable for N frames?}
  S -->|yes| DB[(SQLite\none row per person per day)]
  S -->|no| UI
  DB --> UI
```

### 1. Enrollment

1. On **Register**, enter a name and email and confirm biometric consent.
2. The camera captures five usable samples. Each one must contain exactly one face that is:
   - large enough
   - sharp enough
   - captured at least 0.45 s after the previous sample, in a different pose
   - the same person as the first sample
3. SFace turns each aligned face into a 128-value embedding. The five embeddings are averaged and normalized into one **face template**.
4. The person gets a profile ID like `VA-000001`.

Only the template is stored, never the photos. Re-enrolling replaces the template, and attendance marks that were pending against the old template are rejected.

### 2. Capture

The browser sends JPEG frames, at most 640 px on the long edge, about 4 times per second over the `/ws/attendance` WebSocket.

- **IP cameras:** the server starts one worker process per stream (`ip_camera_worker.py`). The stream URL reaches the worker on stdin, so credentials never appear in the process list or the logs. Only private IPv4 addresses are accepted.
- **Phone in QR mode:** the phone streams frames through a separate HTTPS listener that has no access to the database or admin routes.

### 3. Detection and recognition (`vision_models.py`)

1. **YuNet** finds every face and returns a box and a detection confidence.
2. Faces smaller than 72 px or too blurry are skipped. The skip is reported to the UI ("Move closer", "Hold still").
3. **SFace** aligns each remaining face and produces its embedding.
4. The embedding is compared with every enrolled template using cosine similarity, a score from −1 to 1.

### 4. Decision (`vision_attendance.py`)

A face counts as a match only when all of these hold:

| Rule | Default | Purpose |
|---|---|---|
| Best similarity ≥ threshold | 0.363 (configurable) | Rejects strangers |
| Best − second-best ≥ 0.05 | fixed | Rejects look-alikes and close ties |
| No other face in the frame matched the same profile | — | Rejects duplicates |
| Same profile matched for N consecutive frames | 3 (configurable, 2–8) | Rejects one-frame flukes |

When all rules pass, the server writes the check-in. The timestamp comes from the server clock, not the browser. The calendar date comes from `VISIONATTEND_TIMEZONE` (default `Asia/Kolkata`).

### 5. Storage (`vision_db.py`)

SQLite in WAL mode, with four tables:

- `users`
- `face_templates`
- `attendance`, with `UNIQUE (user_uuid, attendance_date)`
- `settings`

Deleting a profile also deletes its template and its attendance records.

## Run locally

Requirements:
- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- Node.js 22.12 or newer

From the repository root:

```sh
uv sync --locked
uv run python download_models.py   # fetches YuNet + SFace and verifies SHA-256
cd frontend
npm ci
npm run build
cd ..
uv run python run_visionattend.py
```

Then:

1. Open **http://127.0.0.1:8000**.
2. **Register** a person.
3. Open **Attendance** and start a camera.
4. See the results in **Records**, or use **Export CSV**.

The app listens on `127.0.0.1` only. Requests from any other host or origin are rejected.

For frontend development:
1. Run `uv run python run_visionattend.py` in one terminal.
2. Run `npm run dev` in `frontend`.
3. Open **http://127.0.0.1:5173**.

To use another port: `--port 8001`, plus `VISIONATTEND_BACKEND_PORT=8001` for Vite.

## Camera sources

### IP camera or phone camera app (easiest)

1. Put the camera or phone and the laptop on the same Wi-Fi.
2. Start the camera's stream. On Android, the **IP Webcam** app shows an address like `http://192.168.1.20:8080`.
3. In VisionAttend, open **Attendance**, click **IP camera**, and enter the stream URL:
   - IP Webcam (MJPEG): `http://192.168.1.20:8080/video`
   - RTSP camera: `rtsp://192.168.1.20:554/live`
   - With a login: `http://user:pass@192.168.1.20:8080/video`

Notes:

- Use the device's real **private IPv4** address. Hostnames and public IPs are rejected.
- **Use `http://` for phone apps.** Their HTTPS uses a self-signed certificate, which the camera worker rejects on purpose.
- HTTP streams must be MJPEG (`multipart/x-mixed-replace`). A single-snapshot URL like `/shot.jpg` won't work.
- Keep the camera app in the foreground. Android may stop the server in the background.
- The URL is kept in memory for quick reconnects. It is never saved or logged.

### Phone browser over QR pairing

Phone browsers allow camera access only over trusted HTTPS, so this mode needs a certificate.

1. Find the laptop's Wi-Fi IPv4 address with `ipconfig` (Windows) or `ip -4 addr` (Linux). The examples below use `192.168.1.42`.
2. Create a certificate with [mkcert](https://github.com/FiloSottile/mkcert):

   ```sh
   mkcert -install
   mkcert -cert-file camera.pem -key-file camera-key.pem 192.168.1.42
   mkcert -CAROOT
   ```

3. Copy only `rootCA.pem` to the phone and install it as a trusted CA.
   - On iOS, also enable full trust under **Settings → General → About → Certificate Trust Settings**.
   - Never copy `rootCA-key.pem` or `camera-key.pem` off the laptop.
4. Start the app in phone mode:

   ```sh
   uv run python run_visionattend.py --phone-ip 192.168.1.42 --cert camera.pem --key camera-key.pem
   ```

5. Allow TCP 8443 on the private network if the firewall asks.
6. On the laptop, click **Pair phone**. Scan the QR code with the phone and tap **Start camera**.
   - Each pairing link works once and expires after 2 minutes.

If the phone can't connect, check:
- the IP address
- that the phone trusts the certificate
- the firewall
- whether the router has Wi-Fi client isolation turned on (common on guest networks)

## Tuning accuracy

Change these in **Settings**:

- **Minimum cosine similarity.** Higher is stricter: fewer wrong matches, more missed check-ins. The default 0.363 comes from OpenCV's [SFace benchmark](https://docs.opencv.org/4.x/d0/dd4/tutorial_dnn_face.html). Values around **0.45–0.55** suit attendance better. Validate with your own people, cameras, and lighting.
- **Consecutive matching frames.** More frames are safer but take longer to confirm.

For best results:
- Mount the camera at face height, 1–2 m from where people pass, with even lighting.
- Enroll people with the same camera you use for attendance.

## Security and privacy

- **Network:** the admin UI and API listen only on `127.0.0.1`. Host and Origin checks block DNS rebinding and cross-site requests. The phone listener is a separate camera-only HTTPS endpoint.
- **IP cameras:**
  - Each stream is read in an isolated worker process.
  - Only private IPv4 addresses are allowed, and HTTP redirects are refused.
  - Frame and buffer sizes are capped.
- **Data:**
  - Only face templates and attendance rows are stored, never images.
  - Consent time is recorded at enrollment.
  - CSV export escapes formula injection.
- **Not included (demo scope):** authentication, liveness or anti-spoofing, encryption at rest, audit logging, and accuracy validation for a real deployment.

## Project layout

```
app.py                 FastAPI app: REST API, WebSockets, origin checks
run_visionattend.py    Launcher (loopback app + optional phone HTTPS listener)
vision_models.py       YuNet/SFace loading, frame validation, face analysis
vision_attendance.py   Live matching and stable check-in logic
vision_sessions.py     Temporary enrollment sessions
vision_db.py           SQLite schema and queries
vision_ip_camera.py    IP camera WebSocket + worker management
ip_camera_worker.py    Isolated RTSP/MJPEG reader
vision_phone.py        Phone pairing relay
download_models.py     Model download with checksum verification
frontend/              React + Vite + TypeScript UI
tests/                 Backend tests (pytest)
```

## API

Interactive docs are at **http://127.0.0.1:8000/docs** while the app runs. Main endpoints:

| Endpoint | Purpose |
|---|---|
| `POST /api/enrollment/start`, `/{id}/sample`, `/{id}/complete` | Enroll a face |
| `WS /ws/attendance` | Send live frames, receive faces and check-in receipts |
| `WS /ws/ip-camera` | Relay an RTSP/MJPEG camera preview |
| `GET /api/attendance`, `/api/attendance.csv` | Filtered records and CSV export |
| `GET/PATCH /api/settings` | Threshold and stable-frame settings |

## Tests

```sh
uv run pytest
cd frontend
npm test
npm run build
```

The tests use temporary databases, fake embeddings, and simulated cameras. GitHub Actions runs them on every push and pull request. To validate capture and matching quality for a device, you still need a real camera.

If Windows blocks pytest's temp directory, run `uv run pytest --basetemp data/pytest-local`.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `VISIONATTEND_DB` | `data/visionattend.db` | SQLite database path |
| `VISIONATTEND_TIMEZONE` | `Asia/Kolkata` | IANA zone used for attendance dates |
| `VISIONATTEND_BACKEND_PORT` | `8000` | Backend port for the Vite dev proxy |

## Credits and license

The face models come from the [OpenCV Model Zoo](https://github.com/opencv/opencv_zoo):

- [YuNet](https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet)
- [SFace](https://github.com/opencv/opencv_zoo/tree/main/models/face_recognition_sface)

The weights are downloaded at setup time and are not part of this repository.

VisionAttend is released under the [MIT License](LICENSE).
