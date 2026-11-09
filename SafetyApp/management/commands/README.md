# Safety and SOS scheduling

## Safety Timer and Journey check-ins

Use exactly one recurring entry point:

```powershell
.\.venv39\Scripts\python.exe manage.py process_safety_work
```

This one-shot command processes Journey and Safety Timer check-ins, recovers
interrupted SOS notification attempts, and processes due SOS escalations. It
is safe to run repeatedly; database conditional transitions, attempt claims,
and existing SOS event leases fence duplicate processing. Delayed check-ins
and escalations are recovered on the next run. Do not separately schedule
`process_overdue_journeys`, `process_expired_safety_timers`, or
`process_sos_escalations` when using this unified command.

### Windows local-development schedule

1. Open **Task Scheduler** and choose **Create Task**.
2. On **General**, name it `WomenSafety - Process Safety Work`. Run it as the
   Windows account that owns the workspace and its environment configuration.
3. On **Triggers**, create a daily trigger starting now and set **Repeat task
   every** to `1 minute`, **for a duration of** `Indefinitely`.
4. On **Actions**, choose **Start a program**:
   - Program/script: `C:\path\to\project\.venv39\Scripts\python.exe`
   - Add arguments: `manage.py process_safety_work`
   - Start in: `C:\path\to\project`
5. On **Settings**, select **If the task is already running: Do not start a new
   instance**. Enable the task and save it.
6. First run the command manually in PowerShell from the project directory.
   Confirm it exits successfully and reports the expected counts before
   enabling the recurring task. Inspect **Last Run Result** and Task Scheduler
   history after the first scheduled invocation.

Replace `C:\path\to\project` with the actual repository directory. The command
does not launch a server and must not be attached to a browser page.

### Production deployment requirements

Run the same one-shot command from a durable scheduler (for example, a
Kubernetes CronJob with `concurrencyPolicy: Forbid`, or a managed scheduler)
at one-minute intervals. Configure a supported shared database such as
PostgreSQL, persistent logs/alerts for non-zero exits, a bounded job timeout,
and monitored scheduler liveness. Keep provider credentials in the
deployment's secret manager. Do not use Django's development server as the
scheduler or application server. Validate lease contention and recovery
against the actual production database before enabling multiple workers;
local SQLite tests are not proof of multi-process production locking.

The application does not run background phone monitoring. In-app reminders
and browser notifications can only be presented while an authenticated page
is open and connected. Browser notifications require the user to grant browser
permission. `ACCEPTED` on a user reminder records browser-confirmed
presentation (`IN_APP` or `BROWSER`); it does not mean the user read,
acknowledged, or acted on it. If no account email verification and explicit
reminder-email consent flow exists, the account email is not used for these
reminders. Guardian email remains a separate post-grace channel and is sent
only to selected active contacts with verified email and recorded consent.

Defaults are a 15-minute check-in interval, 15-minute grace period, one
check-in reminder retry after five minutes, and automatic SOS disabled. A user
may explicitly opt into using the existing SOS workflow after a further
15-minute unconfirmed period. An SMTP `ACCEPTED` attempt means the SMTP server
accepted the message, not that the recipient received or read it. Outcomes
that may have sent a message but lack a definite response are `UNCERTAIN` and
are not automatically retried.

The complete SafetyApp migration chain is `0001`–`0019`. It includes the
schema used by the unified check-in and SOS workflow. Migrations 0011 and 0013
import `SafetyApp/migration_support_0011.py`; deploy that helper with the
migrations. Do not cherry-pick only the last migrations or omit their earlier
dependencies.

Migration state is per database. In the local development database inspected
on 2026-11-10, SafetyApp migrations `0001`–`0019` were recorded as applied.
That is only a local observation; production migration state has not been
independently verified and must not be inferred from it. Before a production
migration, inspect the target database using its deployment configuration:

```powershell
python manage.py showmigrations SafetyApp
```

This project uses Django 2.1.7, whose `migrate` command does not support
`--plan`; review the pending migration files and their dependencies alongside
the `showmigrations` output. Investigate unexpected applied/pending entries
and data conflicts, and take a verified database backup/snapshot. Apply schema
changes only as an explicit deployment step:

```powershell
python manage.py migrate
```

Do not delete, flush, reset, or roll back the database as a routine response to
migration errors. The active-SOS uniqueness migration intentionally fails if
existing duplicate ACTIVE events need review; preserve the records and resolve
that condition through an explicit data review before retrying.

SOS escalation uses database-backed event leases to prevent simultaneous
workers from advancing the same event. Notification attempts have separate
database-backed claims with fencing tokens, generations, and expirations.
Expired email attempts are recovered as `UNCERTAIN`; they are not
automatically resent. Unconfirmed browser-reminder presentation claims are
offered again with the same idempotency key and browser notification tag.

Provider calls run outside database transactions and must finish before the
attempt lease expires. A provider call that can exceed five minutes requires a
renewal/heartbeat implementation that verifies the current claim. Provider
timeouts or interrupted calls can have unknown outcomes; resolve them through
provider-side reconciliation before retrying. Unknown outcomes remain
`UNCERTAIN`.

Email remains a mock. SMS and voice default to mocks and can be selected
independently; real-provider flags are disabled by default. Do not configure
delivery providers through this command. SQLite is intended for local
development and isolated tests, not to prove multi-worker locking behavior.
Before enabling multiple workers in production, run lease-contention and
recovery tests against the intended PostgreSQL database and validate provider
idempotency and reconciliation behavior.

## Exotel SMS and voice integration

The mock SMS and voice providers are enabled by default. To enable a real
provider, set its Exotel flag to `true` and explicitly set its matching mock
flag to `false`. If the mock flag is omitted, the code treats it as disabled
when the real flag is enabled, preserving the existing SMS opt-in behavior.
Enabling both modes for one channel is an error; disabling both makes that
channel fail with `PROVIDER_DISABLED`. All settings are read from the process
environment at runtime; the application does not load `.env`.

Provider-selection flags:

- `SOS_MOCK_SMS_ENABLED` (defaults to `true` when Exotel SMS is not enabled)
- `SOS_EXOTEL_SMS_ENABLED` (defaults to `false`)
- `SOS_MOCK_VOICE_ENABLED` (defaults to `true` when Exotel voice is not enabled)
- `SOS_EXOTEL_VOICE_ENABLED` (defaults to `false`)

SMS settings:

- `SOS_EXOTEL_API_KEY`
- `SOS_EXOTEL_API_TOKEN`
- `SOS_EXOTEL_ACCOUNT_SID`
- `SOS_EXOTEL_REGION` (`in` or `sg`)
- `SOS_EXOTEL_SMS_SENDER_ID`
- `SOS_EXOTEL_SMS_BODY` (approved message body, at most 2,000 characters)
- `SOS_EXOTEL_DLT_ENTITY_ID` for the India region
- `SOS_EXOTEL_DLT_TEMPLATE_ID` when required by the approved India template

Enable SMS with `SOS_EXOTEL_SMS_ENABLED=true` and
`SOS_MOCK_SMS_ENABLED=false`. The trial account's `EXOSMS` predefined-template
behavior was not verified from the published API contract, so this adapter
does not claim to send that template. Custom SOS SMS requires the configured
sender ID, approved message body, and applicable DLT registrations.

Voice settings:

- `SOS_EXOTEL_VOICE_CALLER_ID` (the account's ExoPhone/virtual number)
- `SOS_EXOTEL_VOICE_APP_ID` (the call-flow ID from Exotel App Bazaar)

Enable voice with `SOS_EXOTEL_VOICE_ENABLED=true` and
`SOS_MOCK_VOICE_ENABLED=false`. Voice uses Exotel's documented
`Calls/connect` call-flow request, with the verified emergency-contact number
as `From`, the configured ExoPhone as `CallerId`, and the account/app-specific
flow URL as `Url`. The configured flow must be reviewed and approved to
communicate the emergency alert safely. A successful response with a call SID
and an `in-progress` or `queued` status is recorded as `ACCEPTED`; it does not
prove that the recipient answered or heard an announcement.

Both adapters use HTTP Basic authentication, a 10-second request timeout, and
the shared Exotel API key, token, account SID, and region settings. A provider
4xx response is treated as a rejection. Transport failures, provider 5xx
responses, and malformed success responses are `UNCERTAIN`. Provider
acceptance is recorded as `ACCEPTED`, not `DELIVERED`. The attempt idempotency
key is passed through Exotel's documented `CustomField` for correlation only;
Exotel's documentation does not establish it as an idempotency guarantee.
Reconciliation remains `UNKNOWN`, so uncertain attempts are not automatically
resent. No delivery/status callback is configured because a callback
authenticity contract has not been verified.

Values must be supplied by a secure deployment secret/configuration mechanism;
never put credentials in source control. No real Exotel request has been made
as part of development or automated tests. Before enabling delivery, provision
and verify the account and applicable KYC/approval requirements, region,
ExoPhone, and app flow. For India SMS, Exotel documents DLT entity
registration, approved templates, and sender ID registration as requirements.

Reference contracts:

- [Exotel SMS overview](https://developer.exotel.com/docs/sms-api/overview)
- [Exotel Send SMS API](https://developer.exotel.com/docs/sms-api/api-reference/send-sms)
- [Exotel Programmable Voice](https://developer.exotel.com/docs/voice)
- [Exotel Connect Number to Call Flow](https://developer.exotel.com/docs/voice-v1/api-reference/connect-to-flow)
- [Exotel Voice status callback](https://developer.exotel.com/docs/voice-v1/api-reference/status-callback)
