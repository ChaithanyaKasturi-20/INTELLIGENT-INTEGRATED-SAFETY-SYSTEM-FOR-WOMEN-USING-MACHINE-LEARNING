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
| SOS | A logged-in user can create an SOS record, optionally including browser-provided coordinates, and cancel or resolve an active event. It does not dispatch police, ambulance, or other emergency services. |
| SOS email | Attempts to send an email to active emergency contacts using Gmail SMTP when configured. The page records send/failure status; SMTP acceptance is not proof the recipient received or read a message. |
| Safety Timer | A user can start a timer, mark themselves safe, or cancel it. When the timer is overdue, the application checks expiry when the Safety Timer page is requested and can create an SOS event. There is no background scheduler or guaranteed automatic dispatch while the app is not being used. |
| Crime data and state selector | Reads the bundled women-crime dataset. The current state form displays the first matching row’s `Rape` value; despite the page label, the handler does not run a live crime-prediction model. |
| Crime heatmap | Visualizes the bundled state-level data and 2013 district-level data by category/year. These are historical aggregate records, not live or street-level crime reports. |
| Route map and navigation | Uses the browser’s location, destination geocoding, and OSRM driving routes; the page can track location during in-page navigation. Routes are not crime-risk-ranked or guaranteed safe. Backgrounding or locking a phone may interrupt browser location updates. |
| Nearby facilities | Queries OpenStreetMap/Overpass for mapped police stations, hospitals, pharmacies, fire stations, and clinics within approximately 5 km. Results depend on OpenStreetMap coverage and the public service’s availability. |
| Neural-network accuracy chart | The `TrainML` view reads a saved `model/model_history.pckl` artifact and plots its training/validation accuracy. It displays existing history; it does not train a model at request time. |

## Screenshots

The repository includes [SCREENS.docx](./SCREENS.docx), which contains UI screenshots. Individual screenshot image files are not currently included in the README.

## Technology and external services

| Area | Verified implementation |
| --- | --- |
| Backend | Python 3.9 project setup; Django 2.1.7 |
| Data | SQLite (`db.sqlite3`), with Django migrations for app models and a separate registration table created by application code |
| Frontend | Django templates, HTML, CSS, and browser-side JavaScript |
| Data/visualization libraries | Pandas, NumPy, Matplotlib, and Seaborn are imported by the application |
| Maps | Leaflet, OpenStreetMap tiles, Nominatim geocoding, Overpass nearby-place queries, and the public OSRM routing endpoint |
| Crime data | `Dataset/CrimesOnWomenData.csv` and the root-level `dstrCAW_2013.csv` |
| Saved chart data | `model/model_history.pckl` |

The current application source does not import TensorFlow, Keras, h5py, scikit-learn, or PyMySQL. Those packages appear in the legacy `requirements.txt`, but are not needed by the application paths described here. In particular, the configured database is SQLite, not MySQL.

The mapping services are public third-party endpoints. Their availability and permitted use are subject to their policies:

- [OpenStreetMap tile usage policy](https://operations.osmfoundation.org/policies/tiles/)
- [Nominatim usage policy](https://operations.osmfoundation.org/policies/nominatim/)
- [Overpass API documentation](https://dev.overpass-api.de/overpass-doc/en/)
- [OSRM project](https://project-osrm.org/)

The public OSM tile service is best-effort and has no availability guarantee; it requires visible attribution and reasonable use. The public Nominatim service requires light usage (at most one request per second), an identifying User-Agent/Referer, and attribution. Overpass and the public OSRM demo service can also be rate-limited or unavailable. Do not use these shared endpoints for high-volume or production traffic.

## Prerequisites

- Windows 10/11 with PowerShell, Git, and the Python launcher (`py`).
- Python 3.9 for the documented application environment. Django 2.1.7 is an old release and is not appropriate for an internet-facing production deployment.
- A browser that supports JavaScript and browser geolocation.
- An internet connection for CDN-hosted Leaflet resources, OpenStreetMap tiles, and the geocoding, routing, and nearby-place services.
- Optional: a Gmail account with an App Password if you want to try SOS email notifications.

The project’s `requirements.txt` is a legacy list of shell commands beginning with `pip install`; it is not in the format accepted by `pip install -r`. It also pins an old TensorFlow 1.14 stack, which is not part of the current app imports and is not suitable for the Python 3.9 quick-start environment. The commands below install the packages used by the current application with the Python 3.9-compatible versions verified for this project.

## Installation and configuration

Run these commands in PowerShell:

```powershell
git clone https://github.com/ChaithanyaKasturi-20/INTELLIGENT-INTEGRATED-SAFETY-SYSTEM-FOR-WOMEN-USING-MACHINE-LEARNING.git
cd INTELLIGENT-INTEGRATED-SAFETY-SYSTEM-FOR-WOMEN-USING-MACHINE-LEARNING

py -3.9 -m venv .venv
.\.venv\Scripts\Activate.ps1

python -m pip install --upgrade pip
python -m pip install Django==2.1.7 numpy==1.26.4 pandas==2.2.3 matplotlib==3.9.2 seaborn==0.13.1
```

These are the runtime packages directly imported by the app. The old TensorFlow/Keras packages listed in `requirements.txt` are not needed to run the current pages; the chart endpoint uses a saved pickle file instead.

If PowerShell blocks activation, allow it only for the current PowerShell process, then activate:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

This does not change the machine-wide execution policy. Alternatively, skip activation and invoke the environment’s Python directly:

```powershell
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install Django==2.1.7 numpy==1.26.4 pandas==2.2.3 matplotlib==3.9.2 seaborn==0.13.1
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
| `/Route` | Route map, location, and nearby facilities |
| `/EmergencyContacts` | Emergency-contact management |
| `/Panic` or `/SOS` | SOS dashboard |
| `/SafetyTimer` | Safety Timer |
| `/TrainML` | Saved accuracy-history chart |

## Testing and verification

Run Django’s system check and app test suite with:

```powershell
python manage.py check
python manage.py shell -c "from SafetyApp.views import ensure_register_table; ensure_register_table()"
python manage.py test SafetyApp
```

The registration-table initialization is needed in a fresh clone because several tests access the project-root SQLite table directly before the normal login/registration flow creates it. The tests also insert test registration rows outside Django’s isolated test database. Run these commands in a clean clone or disposable copy before adding personal data; do not point tests at a database containing user data.

Verified in a disposable copy on 2026-10-11: all 40 tests passed after migrations and registration-table initialization. Running the suite against a fresh database without that initialization produced 12 errors because the tests queried a missing `register` table.

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
│   ├── CrimesOnWomenData.csv
│   └── model_history.pckl
├── model/
│   └── model_history.pckl
├── Safety/
│   ├── settings.py
│   ├── urls.py
│   ├── asgi.py
│   └── wsgi.py
└── SafetyApp/
    ├── migrations/
    │   ├── 0001_initial.py
    │   ├── 0002_sosevent.py
    │   ├── 0003_auto_20260912_2319.py
    │   └── 0004_safetytimer.py
    ├── static/
    ├── templates/
    ├── admin.py
    ├── apps.py
    ├── models.py
    ├── urls.py
    ├── views.py
    ├── tests.py
    ├── test_emergency_contacts.py
    └── test_safety_timer.py
```

- `Safety/` contains Django settings and project-level routing/entry points.
- `SafetyApp/` contains the application models, views, routes, migrations, templates, static files, and tests.
- `Dataset/CrimesOnWomenData.csv` and `dstrCAW_2013.csv` provide the bundled historical crime data.
- `model/model_history.pckl` stores the accuracy history plotted by `TrainML`.
- The root `db.sqlite3`, `.env`, and virtual environments are local files and are not included in this tree.
- `Dataset/model_history.pckl` is also present in the repository, but the current chart view reads `model/model_history.pckl`.

## Security and privacy

This repository is a prototype, not a production-ready safety or authentication service.

- The custom registration code stores passwords directly in SQLite rather than using Django’s password hashing. Use only disposable demo credentials; never reuse a real password.
- The settings currently contain a hard-coded Django `SECRET_KEY`, set `DEBUG = True`, and explicitly allow a demo tunnel host. Do not deploy these settings publicly as-is. Production must use a secret supplied outside source control, `DEBUG = False`, an explicit `ALLOWED_HOSTS` list, HTTPS/security settings, and a reviewed deployment configuration.
- Review authentication and authorization before deployment. The custom account flow is not Django’s standard authentication backend.
- SOS coordinates, emergency-contact information, and registration data are sensitive. Use fictional demo data and restrict database/file access.
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
| `pip install -r requirements.txt` fails | The checked-in file contains `pip install ...` shell commands, not valid requirement entries. Use the explicit runtime installation commands in this README. |
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
