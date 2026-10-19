# Intelligent Integrated Safety System for Women Using Machine Learning

A Django student project exploring women’s safety tools alongside historical crime-data visualizations. It combines account screens, emergency-contact and SOS workflows, a timed safety check-in, maps, and state/district crime data in one web application.

This is a prototype for learning and demonstration. It does not guarantee personal safety, provide verified live crime intelligence, contact emergency services, or replace official emergency resources.

## Contents

- [Overview and features](#overview-and-features)
- [Screenshots](#screenshots)
- [Technology and external services](#technology-and-external-services)
- [Prerequisites](#prerequisites)
- [Installation and configuration](#installation-and-configuration)
- [Database setup](#database-setup)
- [Run locally](#run-locally)
- [Routes](#routes)
- [Testing and verification](#testing-and-verification)
- [Project structure](#project-structure)
- [Security and privacy](#security-and-privacy)
- [Limitations and future work](#limitations-and-future-work)
- [Troubleshooting](#troubleshooting)
- [Contributing](#contributing)

## Overview and features

The intended users are developers and evaluators exploring a prototype for safety-related workflows and crime-data presentation. Safety workflows (contacts, SOS, and timed check-ins) are separate from the historical crime charts; the charts are not used to verify a route’s safety.

| Feature | Current behavior and limitations |
| --- | --- |
| Registration and login | Uses a project-specific SQLite `register` table. This is not Django’s built-in authentication system; passwords are stored as entered, so do not use real or reused passwords. |
| Emergency contacts | Logged-in users can add, edit, activate/deactivate, select a primary contact, and delete contacts. Contact information is stored locally in SQLite. |
| SOS | A logged-in user can create an SOS record with browser-provided coordinates when available, then cancel or resolve it. While the active SOS page remains open, the browser reports location changes to the same SOS event. It does not dispatch police, ambulance, or other emergency services. |
| SOS live location | Active email contacts receive one expiring, token-protected map link when the public HTTPS origin is configured. It displays the latest position and whether it is stale; the token is revoked on SOS resolution/cancellation. Browser/device suspension can pause tracking. |
| SOS email | Attempts an initial alert to active emergency contacts using the existing Gmail SMTP configuration. The message includes the activation time and available initial coordinates/map link; a secure live-location link is optional and appears only when a valid public HTTPS origin is configured. The page records accepted/failed status; SMTP acceptance is not proof the recipient received or read a message. |
| Safety Timer | A user can start a timer, mark themselves safe, or cancel it. When the timer is overdue, the application checks expiry when the Safety Timer page is requested and can create an SOS event. There is no background scheduler or guaranteed automatic dispatch while the app is not being used. |
| Crime data and state selector | Reads the bundled women-crime dataset. The current state form displays the first matching row’s `Rape` value; despite the page label, the handler does not run a live crime-prediction model. |
| Crime heatmap | Visualizes the bundled state-level data and 2013 district-level data by category/year. These are historical aggregate records, not live or street-level crime reports. |
| Safe Places and navigation | The existing Leaflet map retains destination search, OSRM route previews, GPS tracking, rerouting, and in-page navigation. Nearby police stations, hospitals, pharmacies, fire stations, and clinics are shown only when mapped in OpenStreetMap; routes are not crime-risk-ranked or guaranteed safe. Backgrounding or locking a phone may interrupt browser location updates. |
| Plan Journey | A signed-in user can save a route preview, endpoints, estimated distance/duration, intended departure and expected arrival, then resume its route preview/navigation or end/cancel it. Estimates depend on OSRM and are not live traffic guarantees. |
| Journey Guardian | A journey can opt in to a scheduled overdue check-in notice sent once to the selected active emergency contacts with email addresses. Users can check in, extend the deadline, or end a journey. It does not activate SOS. The scheduler is not started automatically; setup is required as described below. Email status is recorded, but SMTP acceptance does not prove delivery. |
| Community Reports | Signed-in users can submit an incident category, description, past incident time, approximate map/device location, and optional JPEG/PNG evidence (5 MB maximum). Only Django staff-approved reports are public. Approval is moderation, not independent verification; reporter identity and evidence are excluded from the public feed/map. |
| Neural-network accuracy chart | The `TrainML` view reads a saved `model/model_history.pckl` artifact and plots its training/validation accuracy. It displays existing history; it does not train a model at request time. |

## Screenshots

The repository includes [SCREENS.docx](./SCREENS.docx), which contains UI screenshots. Individual screenshot image files are not currently included in the README.

## Technology and external services

| Area | Verified implementation |
| --- | --- |
| Backend | Python 3.9 project setup; Django 2.1.7 |
| Data | SQLite (`db.sqlite3`), with Django migrations for app models and a separate registration table created by application code |
| Frontend | Django templates, HTML, CSS, and browser-side JavaScript |
| Data/visualization libraries | The application imports Pandas, NumPy, Matplotlib, and Pillow; the standalone `test.py` analysis script imports Seaborn. |
| Maps | Leaflet, OpenStreetMap tiles, Nominatim geocoding, Overpass nearby-place queries, and the public OSRM routing endpoint |
| Crime data | `Dataset/CrimesOnWomenData.csv` and the root-level `dstrCAW_2013.csv` |
| Saved chart data | `model/model_history.pckl` |

The current application source does not import TensorFlow, Keras, h5py, scikit-learn, or PyMySQL. Those packages were listed in an obsolete dependency file but are not needed by the current application paths. In particular, the configured database is SQLite, not MySQL.

The mapping services are public third-party endpoints. Their availability and permitted use are subject to their policies:

- [OpenStreetMap tile usage policy](https://operations.osmfoundation.org/policies/tiles/)
- [Nominatim usage policy](https://operations.osmfoundation.org/policies/nominatim/)
- [Overpass API documentation](https://dev.overpass-api.de/overpass-doc/en/)
- [OSRM project](https://project-osrm.org/)

The public OSM tile service is best-effort and has no availability guarantee; it requires visible attribution and reasonable use. The public Nominatim service requires light usage (at most one request per second), an identifying User-Agent/Referer, and attribution. Overpass and the public OSRM demo service can also be rate-limited or unavailable. Do not use these shared endpoints for high-volume or production traffic.

Community report maps use Leaflet and OSM tiles. Their content is user-submitted, moderator-approved allegations, not a live or verified crime map.

## Prerequisites

- Windows 10/11 with PowerShell, Git, and the Python launcher (`py`).
- Python 3.9 for the documented application environment. Django 2.1.7 is an old release and is not appropriate for an internet-facing production deployment.
- A browser that supports JavaScript and browser geolocation.
- An internet connection for CDN-hosted Leaflet resources, OpenStreetMap tiles, and the geocoding, routing, and nearby-place services.
- Optional: a Gmail account with an App Password if you want to try SOS email notifications.

`requirements.txt` lists the pinned runtime dependencies used by the current application. The obsolete TensorFlow/Keras and MySQL packages are not required for the current features.

## Installation and configuration

Run these commands in PowerShell:

```powershell
git clone https://github.com/ChaithanyaKasturi-20/INTELLIGENT-INTEGRATED-SAFETY-SYSTEM-FOR-WOMEN-USING-MACHINE-LEARNING.git
cd INTELLIGENT-INTEGRATED-SAFETY-SYSTEM-FOR-WOMEN-USING-MACHINE-LEARNING

py -3.9 -m venv .venv
.\.venv\Scripts\Activate.ps1

python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

The chart endpoint reads its saved accuracy-history pickle; it does not train a neural network at request time.

If PowerShell blocks activation, allow it only for the current PowerShell process, then activate:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

This does not change the machine-wide execution policy. Alternatively, skip activation and invoke the environment’s Python directly:

```powershell
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### Environment variables and email

Copy the placeholder file and edit it locally if you want SOS email:

```powershell
Copy-Item .env.example .env
notepad .env
```

The application reads these settings from the project-root `.env` when it attempts to send an SOS email:

| Variable | Purpose |
| --- | --- |
| `PANIC_EMAIL_USER` | Gmail address used as the SMTP sender |
| `PANIC_EMAIL_PASS` | Gmail App Password used for SMTP authentication; do not use your normal Gmail password |

Without both values, the SOS record can still be created, but email delivery is reported as not configured/failed. Invalid credentials, network errors, or recipient mail-server handling can also prevent delivery. A successful SMTP send call is not a delivery receipt.

The source also reads `GOOGLE_MAPS_EMBED_API_KEY` in a legacy helper for building a Google Maps Embed directions URL. The current Route page uses Leaflet and OpenStreetMap-based services and does not require this key for its active map flow. It is not loaded from `.env` by the current route handler. Never put a browser-visible API key in source control; if you use the legacy helper, configure and restrict the key appropriately.

Never commit `.env`, real credentials, API keys, or contact data. `.env` is ignored by Git. The checked-in `.env.example` contains placeholders only.

## Database setup

The project uses SQLite at the repository root:

```powershell
python manage.py migrate
```

No `makemigrations` step is needed for a normal first run; apply the migrations already in `SafetyApp/migrations/`. The application creates its separate `register` table on demand when registration/login code accesses it.

The `0005_auto_20261011_0843.py` migration adds journey plans/activity/check-in audit records and moderated community reports/flags. The `0006_auto_20261011_1154.py` migration adds SOS live-location sharing tokens and the latest-location timestamp. These migrations do not remove existing tables or records. Keep an existing SQLite database in place and apply migrations normally.

`db.sqlite3` is ignored by Git and is not supplied as a portable developer database. Migration creates the Django-managed tables; it does not create a demo account. If a local `db.sqlite3` already exists, it may contain user data—keep it and do not delete or reset it as routine setup.

## Run locally

After installing dependencies and applying migrations:

```powershell
python manage.py check
python manage.py runserver
```

Django’s default development URL is <http://127.0.0.1:8000/>. To use port 8001 instead:

```powershell
python manage.py runserver 127.0.0.1:8001
```

Stop the development server with **Ctrl+C** in its terminal. Do not use Django’s development server for production.

## Routes

Routes below are defined in `SafetyApp/urls.py`; some features redirect to the login page unless a user session exists.

| Path | Page/action |
| --- | --- |
| `/` or `/index.html` | Home |
| `/UserLogin.html` | Login |
| `/Register.html` | Registration |
| `/Dashboard` | User dashboard |
| `/CrimePredict` | State selector |
| `/Heatmap` | Historical crime heatmap |
| `/Route` | Safe Places map, route preview/navigation, and journey planning |
| `/Journeys` | Signed-in user's saved route plans, guardian status, and check-in history |
| `/CommunityReports` | Approved public feed/map and signed-in report submission/flagging |
| `/EmergencyContacts` | Emergency-contact management |
| `/Panic` or `/SOS` | SOS dashboard |
| `/SOS/LocationUpdate` | Authenticated live location update for the signed-in user's active SOS |
| `/SOS/LiveLocation` | Token-protected emergency-contact live-location map |
| `/SafetyTimer` | Safety Timer |
| `/TrainML` | Saved accuracy-history chart |

Journey and report POST actions are listed in `SafetyApp/urls.py`. Community report moderation and private evidence downloads are available only through Django staff/admin accounts (`/admin/`).

### Run Journey Guardian's overdue-check scheduler

The management command processes overdue, opt-in journeys and records notification attempts:

```powershell
python manage.py process_overdue_journeys
```

It does **not** run automatically. For local development, run the command periodically from a second PowerShell window (for example, every minute):

```powershell
while ($true) {
    python manage.py process_overdue_journeys
    Start-Sleep -Seconds 60
}
```

Stop the loop with **Ctrl+C**. For production, configure and monitor an OS/cloud scheduler to invoke the command once per minute using the same application release, environment variables, and database. Do not run overlapping scheduler instances. Email uses the existing `PANIC_EMAIL_USER` and `PANIC_EMAIL_PASS` SMTP configuration; missing configuration or send failures are recorded. One attempt per trusted contact is intentional: interrupted/ambiguous SMTP sends are not retried automatically to avoid duplicate notifications. A scheduled command, SMTP acceptance, or this prototype cannot guarantee that a contact receives or responds to an alert.

Community evidence is stored under the ignored `private_uploads/` directory and is not mapped to a public media URL. Authorized Django staff can download evidence from the moderation screen. Keep that directory private in any deployment and do not configure a web server to serve it directly.

### SOS live-location sharing

The initial SOS email is independent of external live-location sharing. It is sent through the existing `PANIC_EMAIL_USER` / `PANIC_EMAIL_PASS` SMTP setup with the activation time and available coordinates / Google Maps link. If no safe public URL is configured, the email says it contains only the initial location; it is not sent a localhost URL.

To include the optional secure recipient link, set `SOS_PUBLIC_BASE_URL` in the private `.env` file to the application's public HTTPS **origin only**, for example `https://sos.example.org`. Do not add a path, query, fragment, token, username, or password. The app rejects HTTP, localhost, private/loopback IPs, reserved local hostnames, domains that resolve to any non-public address, malformed URLs, and DNS names that cannot be resolved. Restart Django after changing `.env`; the origin is used to build the recipient link and is included in Django's allowed hosts only when it is a syntactically valid HTTPS origin.

Each SOS gets a cryptographically random, event-specific 24-hour access token. Only its SHA-256 hash is stored. The raw token is placed in the URL fragment, removed from the recipient page's address bar immediately, and never sent to the location API as a URL parameter. Every location read checks the token, expiry, revocation, and active SOS state. Responses are non-cacheable and rate-limited. Resolving/cancelling the SOS revokes the link. The owner update endpoint is authenticated and updates only that owner's active SOS.

**Do not point a public tunnel directly at Django port 8000.** The small standard-library proxy in `scripts/sos_location_proxy.py` listens only on `127.0.0.1:8765` and forwards only `GET /SOS/LiveLocation` and JSON `POST /SOS/LiveLocation/Data`. Paths (including query strings and trailing-slash variants) outside that exact allowlist return 404; other methods return 405. It ignores client-supplied host, cookie, authorization, CSRF, and forwarding headers, uses the fixed upstream host `127.0.0.1:8000`, forwards only the JSON content type, and returns only a small response-header allowlist. It does not relay Django 5xx/debug error pages. The recipient data endpoint is read-only and validates its event-specific token; it does not require a session cookie or CSRF token. Django's global CSRF middleware remains enabled, and the owner location-update endpoint is deliberately not exposed by this proxy.

To run a temporary local demo, from the repository root:

1. Keep Django running on `127.0.0.1:8000`. Do not change `DEBUG`, middleware, or Django authentication/CSRF settings for the tunnel.
2. In a PowerShell terminal, start the proxy:

   ```powershell
   py -3.9 scripts\sos_location_proxy.py
   ```

3. In a separate PowerShell terminal, start the Quick Tunnel:

   ```powershell
   cloudflared tunnel --url http://127.0.0.1:8765
   ```

4. Copy only the generated `https://<random-name>.trycloudflare.com` origin into the private `.env` as `SOS_PUBLIC_BASE_URL=https://<random-name>.trycloudflare.com`, then restart Django. Replace the hostname with the actual value Cloudflare printed; never paste secrets or commit the tunnel URL. The hostname may change each time a Quick Tunnel starts. The proxy must be running before the tunnel can reach the app.
5. Stop the proxy with `Ctrl+C` in its terminal. Alternatively, from another PowerShell terminal, stop the listener only if its command line identifies this proxy script:

   ```powershell
   $listener = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
   if ($listener) {
       $owner = Get-CimInstance Win32_Process -Filter "ProcessId = $($listener.OwningProcess)"
       if ($owner.CommandLine -match 'scripts[\\/]+sos_location_proxy\.py') {
           Stop-Process -Id $listener.OwningProcess
       } else {
           throw 'Port 8765 is not owned by the SOS location proxy.'
       }
   }
   ```

The tunnel serves only the two recipient paths allowed by the local proxy; it does not expose `/admin`, authentication, registration, SOS activation, or owner GPS updates. A Quick Tunnel is temporary and is not production infrastructure. This does not itself test location sharing or send any email.

The browser requests permission and coordinates when SOS is activated, then watches for changes while an active SOS page remains open. It posts on movement or at most every two minutes. The browser/device can pause location updates when suspended, locked, offline, or closed; recipients see when the last position is stale. The Safety Timer's existing expiry check creates the same SOS and sharing workflow when the Safety Timer page is visited after expiry; timer expiry is not a background scheduled event.

## Testing and verification

Run Django’s system check and app test suite from a disposable source copy, not from a working directory whose `db.sqlite3` contains personal data. Some legacy tests access the separate `register` table through a direct SQLite connection, outside Django’s temporary test database. Exclude the real `.env`, `db.sqlite3`, and uploaded data from the copy, and clear inherited SMTP environment variables before running tests.

```powershell
python manage.py check
python manage.py shell -c "from SafetyApp.views import ensure_register_table; ensure_register_table()"
python manage.py test SafetyApp
```

The table initialization and test fixtures write to the copy's own SQLite file because the legacy registration table uses direct SQLite access outside Django's temporary test database. Run this only in the disposable copy, never against a database containing personal data. Do not reset or flush a personal database to prepare for tests.

Automated tests do not verify real GPS accuracy, map-provider availability, external API policy compliance, or end-to-end email delivery. Manually test browser location permission, maps, and email using non-sensitive test accounts.

## Project structure

```text
.
├── manage.py
├── requirements.txt
├── .env.example
├── database.txt
├── dstrCAW_2013.csv
├── Dataset/
│   └── CrimesOnWomenData.csv
├── model/
│   └── model_history.pckl
├── Safety/
│   ├── settings.py
│   ├── urls.py
│   └── wsgi.py
└── SafetyApp/
    ├── migrations/
    │   ├── 0001_initial.py
    │   ├── 0002_sosevent.py
    │   ├── 0003_auto_20260912_2319.py
    │   ├── 0004_safetytimer.py
    │   ├── 0005_auto_20261011_0843.py
    │   └── 0006_auto_20261011_1154.py
    ├── static/
    ├── templates/
    ├── admin.py
    ├── apps.py
    ├── models.py
    ├── urls.py
    ├── views.py
    ├── journey_views.py
    ├── journey_notifications.py
    ├── community_views.py
    ├── management/commands/process_overdue_journeys.py
    ├── test_emergency_contacts.py
    ├── test_registration.py
    ├── test_sos_live_location.py
    ├── test_safety_timer.py
    └── test_journey_community.py
```

- `Safety/` contains Django settings and project-level routing/entry points.
- `SafetyApp/` contains the application models, views, routes, migrations, templates, static files, and tests.
- `Dataset/CrimesOnWomenData.csv` and `dstrCAW_2013.csv` provide the bundled historical crime data.
- `model/model_history.pckl` stores the accuracy history plotted by `TrainML`.
- `scripts/` contains the path-restricted SOS recipient proxy and its focused tests.
- The root `db.sqlite3`, `.env`, and virtual environments are local files and are not included in this tree.
- `requirements.txt` contains the current runtime dependency pins. `test.py` is a standalone legacy data-analysis script, not a Django management command.

## Security and privacy

This repository is a prototype, not a production-ready safety or authentication service.

- The custom registration code stores passwords directly in SQLite rather than using Django’s password hashing. Use only disposable demo credentials; never reuse a real password.
- The settings currently contain a hard-coded Django `SECRET_KEY`, set `DEBUG = True`, and explicitly allow a demo tunnel host. Do not deploy these settings publicly as-is. Production must use a secret supplied outside source control, `DEBUG = False`, an explicit `ALLOWED_HOSTS` list, HTTPS/security settings, and a reviewed deployment configuration.
- Review authentication and authorization before deployment. The custom account flow is not Django’s standard authentication backend.
- SOS coordinates, emergency-contact information, and registration data are sensitive. Use fictional demo data and restrict database/file access.
- Journey endpoints are owner-filtered using the existing custom session account; they are not keyed to Django users. Community moderation is separately restricted to Django staff/admin accounts. Do not expose the Django admin or private evidence storage to untrusted users.
- Community locations are rounded to two decimal places before storage, and public entries omit reporter identity and evidence. Report descriptions are user-generated; moderation cannot establish that allegations are true.
- Journey Guardian sends route labels and the expected check-in time only to contacts selected by the user. Overdue status alone never activates SOS. Browser navigation is not guaranteed to continue in the background.
- `.env` and all credentials must remain private. The `.env.example` file contains placeholders, never real values.
- Public Quick Tunnels and the app’s debug configuration are not suitable for confidential data or production use.
- SOS emails do not contact emergency services. In an emergency, use local official emergency services.

## Limitations and future work

Current limitations include historical rather than live crime data, a state endpoint that returns a dataset value instead of a prediction, prototype authentication, dependence on public map services, and no background job for timer expiry.

Possible future improvements (not implemented) include:

- Replace the legacy Django/Python stack and productionize settings.
- Use Django’s built-in authentication and hashed passwords.
- Add robust automated tests and isolate all tests from the development database.
- Improve the reliability and observability of emergency notifications.
- Integrate appropriately licensed, verified crime data and clearly document model validation.
- Improve mobile, accessibility, and background-navigation testing.
- Add configurable/cached map-service integrations with appropriate provider policies.

## Troubleshooting

| Problem | What to check |
| --- | --- |
| `py` or Python is not found | Install Python 3.9 and the Windows Python launcher, reopen PowerShell, and verify with `py -3.9 --version`. |
| Virtual-environment activation is blocked | Use the process-only policy command above, or invoke `.\.venv\Scripts\python.exe` directly. |
| Dependency installation fails | Activate `.venv` and run `python -m pip install -r requirements.txt`; check that the selected interpreter is Python 3.9. |
| Django cannot import a required package | Activate `.venv` and install the runtime package set shown above with `python -m pip install ...`. Avoid installing the legacy TensorFlow 1.14 pins into Python 3.9. |
| Migration reports unapplied migrations | Run `python manage.py migrate` from the repository root and review the full error. Do not delete or flush the database. |
| Missing `register` table | Open the app through its registration/login flow; application code creates this table on demand. |
| SOS email is not sent | Check the `.env` sender address and Gmail App Password, Gmail SMTP access, network connectivity, active contact email, and delivery status shown by the app. Do not share the credentials. |
| Browser location is unavailable | Allow location permission and use localhost or HTTPS. Accuracy depends on the device, browser, and surroundings. |
| Map tiles or nearby places are missing | Check internet access, browser dev tools, map attribution, and public provider availability. OSM/Overpass data can be incomplete or rate-limited. |
| Destination search or route calculation fails | Nominatim geocoding and the public OSRM routing endpoint can fail, throttle, or lack coverage. Retry later or enter a more specific destination. |
| Route tracking does not update | Keep the page active, allow geolocation, and verify that the device reports a location. Browser tracking may pause when backgrounded or locked. |
| Port 8000 is already in use | Start on another local port, for example `python manage.py runserver 127.0.0.1:8001`. |

## Contributing

Contributions should preserve the prototype’s privacy and safety caveats, avoid committing local databases or secrets, and include relevant tests. Before submitting changes, run `python manage.py check` and the app tests in a clean or disposable project copy as described above.
