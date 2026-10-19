# Project Audit and Safe Cleanup

**Audit date:** 2026-10-12

**Scope:** Django application, configuration and dependency files, migrations, templates, static assets, tests, scripts, and repository documentation.

## Executive summary

The source inventory contained 66 files outside Git metadata, virtual environments, and Python bytecode caches. It included 33 Python files, 16 HTML templates, static assets, datasets, local configuration/database files, and documentation. The database, `.env`, user records, uploads, model artifact, datasets, and migration history were preserved.

The changes include unused-import cleanup, demonstrably unreferenced legacy CSS removal, dependency-manifest and setup-documentation corrections, and an SOS public-base-URL validation/conditional-sharing behavior adjustment in `SafetyApp/views.py`. No source files, functions, classes, database records, or migrations were deleted. No duplicate logic was consolidated because no safe consolidation was established.

## Inventory disposition

- **Python source, tests, and migrations (33 files):** retained. No function, class, or application logic block was removed. Only the unused-import statements described below were changed.
- **Templates (16 files):** retained. Route/template references were checked before removing CSS; no template was classified as dead.
- **Static files:** JavaScript, images, and the remaining CSS were retained. Only the verified unused CSS rule blocks were removed. The `Thumbs.db` filesystem/index discrepancy is documented below.
- **Data and local artifacts:** both CSV datasets, the model-history pickle, the SQLite database, the environment files, and the UI screenshot document were retained. Binary contents and private `.env` values were not dumped or reproduced.
- **Project/configuration/docs:** project entry points, routes, settings, migrations, `.gitignore`, `database.txt`, `.env.example`, and existing proxy scripts were retained. `requirements.txt` and `README.md` received the specific corrections listed below; this report is the only new audit artifact.

## Findings and changes

| Area | Finding | Action |
| --- | --- | --- |
| Python imports and SOS public URL handling | `SafetyApp/views.py` had unused `RequestContext`, `date`, and `seaborn` imports; the scaffold `SafetyApp/tests.py` had an unused `TestCase` import. The staged `test.py` cleanup removed `pickle` and `matplotlib.pyplot`; the former is only referenced in a commented-out block, but active plotting code still uses `plt`. The working-tree correction restores the `matplotlib.pyplot` import. The SOS sharing flow also needed to use the validated public base URL consistently. | Removed the unused imports and restored the required plotting import in `test.py`. Updated `SafetyApp/views.py` to read and validate `SOS_PUBLIC_BASE_URL` before enabling or including a live-location share link. If `SOS_PUBLIC_BASE_URL` is explicitly present in the environment, its value takes precedence over the Django setting; an explicitly present but blank value therefore disables the URL rather than falling back to the setting. A follow-up AST scan of the project’s Python files detected no remaining unused imports. |
| Legacy CSS | The sidebar, search form, calendar, and footer rules (including the footer responsive rule) had no corresponding references in templates or client code. The referenced `img04.jpg`/`img05.jpg` assets were absent. | Removed those stale CSS rules. Kept the rest of the stylesheet and verified no matching template/runtime references remained. |
| Dependencies | `requirements.txt` contained shell commands rather than valid requirement specifiers, and included legacy TensorFlow/Keras/MySQL packages not used by the current application. | Replaced it with pinned runtime packages: Django 2.1.7, NumPy 1.26.4, pandas 2.2.3, Matplotlib 3.9.2, Seaborn 0.13.1, and Pillow 10.4.0. No package installation or upgrade was performed as part of the audit. |
| README | Setup instructions referenced an invalid requirements file; project inventory included missing paths; testing guidance did not adequately isolate tests that access the custom SQLite registration table; migration notes omitted migration 0006. | Corrected the install/test guidance, project tree, and migration notes. Added the verified SOS proxy constraints and retained the safety/security caveats. |

## Protected and retained items

- `db.sqlite3`, `.env`, `.env.example`, and all existing user/contact/SOS records were not modified, reset, or replaced. No migration, database setup, `flush`, SOS activation, email send, or recipient notification was performed against the working project.
- Crime datasets, `model/model_history.pckl`, all migrations (including `0006_auto_20261011_1154.py`), and the restricted proxy/test scripts were retained.
- `database.txt` was retained. It appears to describe legacy database setup that does not match the app’s current SQLite configuration; its intended historical use is uncertain.
- The standalone root `test.py` and the scaffold `SafetyApp/tests.py` were retained rather than deleting potentially intentional developer entry points.
- `SafetyApp/static/images/Thumbs.db` appeared in the Git index but was not present in the working-tree file listing. It was left untouched because its actual filesystem state and purpose were uncertain.
- No file was classified as safely deletable. No files were moved or consolidated.

## Risks identified but not changed

The current project settings have a hard-coded Django secret, `DEBUG = True`, and an explicitly allowed demo tunnel hostname. The custom registration system also stores passwords as entered, and Django 2.1.7 is obsolete. These are material security risks and should be addressed in a separate, deliberate deployment/security change; changing them was outside this cleanup and could affect existing local workflows. Secret values are intentionally not reproduced here.

The legacy registration tests use a direct SQLite connection to `db.sqlite3`, outside Django’s temporary test database. Consequently, running the test suite directly in a personal working copy could modify that copy’s database. The README now instructs developers to use a disposable copy, exclude `.env`, `db.sqlite3`, and uploads, clear inherited SMTP environment variables, and initialize only the disposable copy’s `register` table before testing.

## Verification performed

| Check | Result |
| --- | --- |
| Django `manage.py check` in the working copy | Passed; no issues reported. |
| Django `manage.py check` in the disposable copy | Passed; no issues reported. |
| `SafetyApp` test suite in the disposable copy | Passed: 89 tests, 0 failures/errors. |
| Restricted SOS proxy test script | Passed: 9 tests. |
| `node --check SafetyApp/static/sos-location.js` | Passed. |
| Python AST unused-import scan | No unused imports detected. |
| `git diff --check` | Passed. |
| Disposable Python 3.9 install from `requirements.txt` | Passed; all six direct pins and their transitive dependencies installed. |
| `pip check` in disposable environment | Passed; no broken requirements. |
| Django check and migrations in disposable install | Passed; all listed migrations applied in the disposable workspace. |

The first disposable-copy test run, before initializing the custom `register` table in that copy, produced seven `no such table: register` errors. No product defect was inferred from that setup failure. The corrected rerun initialized only the disposable copy’s table and passed all 89 tests. SMTP environment variables were cleared and the real `.env` and database were excluded from the test copy; the suite emitted expected simulated SMTP error-path logs but completed successfully. The disposable copy was removed after the run.

During the final read-only review, the README install flow was repeated in a newly created temporary Python 3.9 virtual environment. The source copy excluded `.env`, `db.sqlite3`, and `private_uploads`; dependency installation, `pip check`, Django's system check, and all migrations succeeded. The disposable workspace and its newly created empty SQLite database remain under the operating system's temporary directory and are outside the repository; no existing user data was copied. No files in that temporary workspace were deleted during the review. Automated checks do not establish real device GPS accuracy, browser background behavior, real external recipient delivery, or live-location behavior on a separate physical device. Do not infer those outcomes from the test results.

## Working-tree boundaries

At the start of this audit, `README.md` and `SafetyApp/views.py` already contained uncommitted changes, and `scripts/` was untracked. Those existing changes were preserved; the pre-existing SOS behavior and proxy files were not reverted. This audit did not commit changes.
