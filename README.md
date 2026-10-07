# hermes-nextcloud

<p align="center">
  <img src="NC_Hermes.png" alt="hermes-nextcloud" width="400"/>
</p>

Connect your self-hosted Nextcloud instance to Hermes Agent. Manage files, notes, calendar events, tasks, and contacts directly from any conversation. No plugins, no external services, just your data.

## Status

![Nextcloud](https://img.shields.io/badge/Nextcloud-yes-green)
![Python](https://img.shields.io/badge/Python-3.8+-blue)
![License](https://img.shields.io/badge/License-MIT-purple)

## What is this?

hermes-nextcloud is a skill for Hermes Agent that wraps the Nextcloud WebDAV, Notes API, CalDAV, and CardDAV protocols into a command-line interface. If you run Nextcloud on your own VPS or home server, you can read and write your data without opening a browser.

Python standard library only: no pip packages, no curl.

## Features

**Files:** List, search, read, upload, append, download, move, and delete files via WebDAV. Overwriting an existing file requires its etag (or `--overwrite`).

**Notes:** List, find, read, create, edit, append to, and delete notes via the Nextcloud Notes API (requires the Notes app).

**Calendar:** List, search, create, edit, shift, and delete events via CalDAV. Times are read in your configured timezone; relative dates like `tomorrow 14:00` work too. Recurring events are listed per occurrence, and a series is never moved by accident. Reminders (push or e-mail, as many as you like) are set on the event itself.

**Tasks:** List (optionally only open), create, edit, complete, reopen, and delete tasks, with reminders before the due date.

**Contacts:** List, search, view, create, edit, delete, and export contacts via CardDAV. Search covers all text fields, ignores case and accents, and ranks the best matches first.

**Safe edits:** Editing an event, task, or contact changes only the fields you pass. Everything else (recurrence, alarms, subtasks, addresses, photos) is kept, and the item is written back to its own URL with an etag check. Replacing a whole file or note requires the etag from when it was read (or an explicit `--overwrite` / `--force`). Concurrent changes are never silently overwritten.

**Setup:** Guided setup validates your URL, login, and app password and stores them in a private file.

## Requirements

- A running Nextcloud instance (any recent version)
- An **App Password** (Settings → Security → Devices & sessions)
- Python 3.8 or newer (3.9+ recommended for timezone support)
- Hermes Agent

## Installation

Make sure the _target_ directory is `nextcloud`:

```bash
cd ~/.hermes/skills/productivity
git clone https://github.com/Redsandro/hermes-nextcloud.git nextcloud
```

## Setup

```bash
python3 ~/.hermes/skills/productivity/nextcloud/scripts/setup.py
```

The script asks for your Nextcloud URL, login name, app password (input hidden), and timezone. It verifies the login, looks up your Nextcloud user id (which can differ from the login name, e.g. when logging in with an e-mail address), and saves everything to `~/.hermes/.env.nextcloud` with mode 600.

```bash
setup.py --check                                    # test saved credentials
setup.py --url URL --user NAME --token-stdin < pw   # non-interactive
```

### Manual configuration

Environment variables override the env file.

```bash
export NEXTCLOUD_URL="https://your-nextcloud.example.com"
export NEXTCLOUD_USER="your_login"
export NEXTCLOUD_TOKEN="your_app_password"
export NEXTCLOUD_TIMEZONE="Europe/Amsterdam"   # optional, default UTC
export NEXTCLOUD_USER_ID="your_user_id"        # optional, only if it differs from the login
```

### Creating an App Password

1. Log in to your Nextcloud instance
2. Go to **Settings → Security → Devices & sessions**
3. Enter a name (e.g. `hermes-agent`) and click **Create new app password**
4. Copy the password. It is shown only once.

## Usage

```bash
NC="python3 ~/.hermes/skills/productivity/nextcloud/scripts/nextcloud_api.py"
$NC check
```

Dates: `YYYY-MM-DD`, `YYYY-MM-DD HH:MM`, or relative: `today`, `tomorrow`, `+90` / `-365` (days), also with a time (`tomorrow 14:00`). Dutch words (`vandaag`, `morgen`, `overmorgen`) work too.

Reminders: `15m`, `1h`, `2d`, `1w` before the start (tasks: before the due date), push by default, `:email` for e-mail. On all-day items `2d` means 2 days before at 09:00. Reminders are stored in the event, not per user: Nextcloud sends them to the calendar owner and everyone with write access (an admin setting, on by default), not to read-only sharees. E-mail reminders need working background jobs (cron) and mail settings on the server.

`calendar list` and `calendar search` always cover a limited period, by default 30 days back to 90 days ahead, and return `{"from", "to", "events"}`.

Every command prints one JSON object, `{"status": "success", "data": ...}` or `{"status": "error", "message": ...}`, and exits with code 1 on error.

### Files

```bash
$NC files list --path /Documents
$NC files search --query budget
$NC files get --path /Notes/todo.md                      # content + etag
$NC files append --path /Notes/todo.md --text "- [ ] milk"
$NC files upload --path /Notes/todo.md --content - --if-match '<etag>' < todo.md
$NC files upload --remote /Documents/report.pdf --local ./report.pdf   # new file; --overwrite to replace
$NC files download --remote /Documents/report.pdf --local ./report.pdf
$NC files move --src /a.txt --dst /b.txt
$NC files delete --path /Documents/old.txt
```

### Notes (requires Nextcloud Notes app)

```bash
$NC notes list                      # without content
$NC notes find --query meeting
$NC notes get --id 941              # content + etag
$NC notes create --title "Meeting notes" --content "Discussed the Q3 roadmap."
$NC notes append --id 941 --text "Follow up next week."
$NC notes edit --id 941 --content - --etag <etag> < note.md   # --etag required (or --force)
$NC notes delete --id 941
```

### Calendar

```bash
$NC calendars list --type events
$NC calendar list --from 2026-07-01 --to 2026-07-31 [--calendar Work]
$NC calendar list --on today
$NC calendar search --query standup --on tomorrow
$NC calendar search --query dentist --from +90 --to +365
$NC calendar shift --uid <uid> --days 1               # keeps duration; --hours/--minutes too
$NC calendar create --summary "Team standup" --start "2026-07-01 09:00" --end "2026-07-01 09:30"
$NC calendar create --summary "Dentist" --start "2026-07-02 10:00" --end "2026-07-02 10:30" --remind 2d --remind 1d:email
$NC calendar create --summary "Holiday" --start 2026-08-03          # all-day
$NC calendar edit --uid <uid> --summary "New title"
$NC calendar edit --uid <uid> --remind 1h             # replaces all reminders; --remind "" removes them
$NC calendar delete --uid <uid>
```

### Tasks

```bash
$NC tasks list --open
$NC tasks create --title "Review pull request" --due 2026-07-05 [--priority 1] [--remind 1d]
$NC tasks edit --uid <uid> --title "Updated title"
$NC tasks complete --uid <uid>
$NC tasks reopen --uid <uid>
$NC tasks delete --uid <uid>
```

### Contacts

```bash
$NC addressbooks list
$NC contacts list [--addressbook Contacts]
$NC contacts search --query jansen
$NC contacts search --query "jan jansen"              # all words must match
$NC contacts search --query "doctor, physician"       # any of the alternatives
$NC contacts list --compact                           # names/organization/title only
$NC contacts get --uid <uid>
$NC contacts create --name "Jan Jansen" --email jan@example.com --phone "+31 6 12345678"
$NC contacts edit --uid <uid> --email new@example.com   # replaces all e-mail addresses
$NC contacts export --uid <uid> --local ./contact.vcf
```

## Project structure

```
hermes-nextcloud/
├── README.md
├── LICENSE
├── SKILL.md                  # Skill manifest and agent instructions
└── scripts/
    ├── nextcloud_api.py      # Main CLI, all commands
    ├── setup.py              # Guided credential setup
    └── requirements.txt      # Empty: standard library only
```

## Security

- Credentials are stored in `~/.hermes/.env.nextcloud`, created with mode 600. Use an **App Password**, never your login password; it can be revoked on its own.
- The password is never passed on a command line (no curl), never printed, and never sent over plain `http://` (unless you set `NEXTCLOUD_ALLOW_HTTP=1`). Redirects are not followed.
- Every HTTP status is checked: a failed request is reported as an error, never as success.
- SKILL.md instructs the agent to treat content from Nextcloud as data, not as instructions.

## Contributing

This fork is tailored for personal use. It's best to contribute upstream.

## License

MIT License. See [LICENSE](LICENSE) for the full text.
