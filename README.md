# Ticket watcher

Keeps an eye on club ticket pages (Chelsea or any other site) while you are logged in
with your own membership, and alerts you the moment tickets look available:

- a push notification on your phone (via the free [ntfy](https://ntfy.sh) app) that opens the page when tapped
- a beep in the terminal
- the match tab brought to the front of the bot's browser window, already logged in, so you can buy straight away

## What it does not do, on purpose

- **It does not buy for you.** It never clicks "buy" or "add to basket". Club ticketing terms
  ban automated purchasing, and Chelsea and other clubs cancel memberships caught using bots.
  You do the last click, it does the waiting.
- **It does not check every millisecond.** The minimum is 10 seconds, default 30. A single page
  load takes around a second anyway, and hammering the site gets your IP and membership flagged.
- **It does not skip queues or solve CAPTCHAs.** If it lands in a waiting room it alerts you and
  stops refreshing that tab, so you keep your place.
- If the site returns 403 or 429 (too many requests), it slows down automatically.

## Setup (on your own computer)

Needs Python 3.9+. The bot has to run on a computer that stays on while you wait.

```bash
pip install -r requirements.txt
playwright install chromium
cp config.example.yaml config.yaml
```

Edit `config.yaml`: paste the match ticket page link, and set an `ntfy_topic` if you want phone alerts.

## Use

```bash
python ticket_watch.py login        # browser opens, log in with your membership, press Enter
python ticket_watch.py test         # checks each page once and shows what it sees + a screenshot
python ticket_watch.py test-notify  # sends a test alert to your phone
python ticket_watch.py watch        # runs until Ctrl+C
```

Your password is never stored by this script. You log in yourself in the browser window, and the
session cookies live in `browser-profile/` on your machine (git-ignored). Only one command can use
that profile at a time, so close `watch` before running `login` again.

## Tuning the "available" check

Every ticket site is different, so you tell the bot what "available" looks like. Pick one under
`available_when`:

| option        | means available when...                        |
|---------------|------------------------------------------------|
| `text_absent` | none of these phrases are on the page          |
| `text`        | any of these phrases appear                    |
| `selector`    | this CSS element is visible on the page        |

Run `python ticket_watch.py test` while the match is sold out: the result should say
`unavailable`. If it says `available`, look at the screenshot in `screenshots/` and adjust the
phrases. `text_absent` is the easiest to set up but can give false alarms if the page fails to load
properly, so `text` or `selector` are more reliable once you know what the buy button says.

After an alert the bot leaves that tab alone (it will never reload a page you might be checking
out in) and checks again in a fresh tab after `hold_minutes`.

## Tests

```bash
pip install pytest
python -m pytest tests
```

Set `CHROMIUM_PATH` if you want to use a Chromium you already have instead of `playwright install`.
