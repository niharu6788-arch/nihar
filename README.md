# Ticket watcher

Keeps an eye on club ticket pages (Chelsea or any other site) while you are logged in
with your own membership. The moment tickets look available it:

- puts them in your basket, if you recorded the clicks for that (see "Basket steps")
- sends a push notification to your phone (via the free [ntfy](https://ntfy.sh) app), tap it to open the page
- beeps in the terminal
- brings the match tab to the front of the bot's browser window, logged in, so you can pay straight away

## What it does not do, on purpose

- **It does not pay or check out.** It stops at the basket. Steps that look like a pay or
  checkout button are refused when the config loads.
- **It does not check every millisecond.** The minimum is 10 seconds, default 30. A single page
  load takes around a second anyway, and hammering the site gets your IP and membership flagged.
- **It does not skip queues or solve CAPTCHAs.** In a waiting room it alerts you and stops
  refreshing that tab so you keep your place, then fills the basket when your turn comes. If a
  robot check appears, it alerts you to solve it yourself.
- It only tries the basket **once per match per run**, so it can never keep adding tickets.
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
python ticket_watch.py record       # optional: record your clicks to "Add to basket"
python ticket_watch.py test-basket  # optional: runs the basket steps once right now
python ticket_watch.py watch        # runs until Ctrl+C
```

Your password is never stored by this script. You log in yourself in the browser window, and the
session cookies live in `browser-profile/` on your machine (git-ignored). Only one command can use
that profile at a time, so stop `watch` before running another command.

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

## Basket steps

The bot does not know the site's layout, so you show it once:

1. Find a match that has tickets on sale right now. You can't record clicking "Add to basket" on a
   sold-out match, and the buttons are usually the same across matches on the same site.
2. Run `python ticket_watch.py record`. A browser opens, plus a Playwright Inspector window.
3. Click through as you normally would: stand, quantity, "Add to basket". Stop there. Each click
   appears as a line of code in the Inspector.
4. Turn those lines into `basket_steps` in `config.yaml`:

| Inspector line                                            | basket step                                    |
|-----------------------------------------------------------|------------------------------------------------|
| `page.get_by_role("button", name="Add to basket").click()` | `- click: 'role=button[name="Add to basket"]'` |
| `page.get_by_text("Matthew Harding Lower").click()`        | `- click: 'text=Matthew Harding Lower'`        |
| `page.locator("#quantity").select_option("2")`             | `- select: {selector: "#quantity", value: "2"}` |
| `page.locator("#seat-123").click()`                        | `- click: '#seat-123'`                         |

5. Optionally end with a `wait_for` step on text that only shows once the basket worked, for example
   `- wait_for: 'text=added to your basket'`. Then a "success" alert really means success.
6. Run `python ticket_watch.py test-basket` on a match with tickets to check it works, then empty
   the basket if you don't want them.

Each step waits up to 15 seconds for its button. If one fails (a page change, a robot check, seats
gone), you get a "FINISH IT YOURSELF" alert and the tab is left for you.

## Tests

```bash
pip install pytest
python -m pytest tests
```

Set `CHROMIUM_PATH` if you want to use a Chromium you already have instead of `playwright install`.
