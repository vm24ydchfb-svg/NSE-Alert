# NSE High-Impact News → Telegram

A free alert bot that checks NSE corporate announcements every 5 minutes on weekdays and sends potentially market-moving public filings to Telegram.

## What it does

- Reads NSE's corporate-announcement feed.
- Looks at the latest 15 minutes to compensate for GitHub Actions schedule jitter.
- Scores keywords such as large orders, acquisitions, fund raising, ratings, approvals, promoter activity, regulatory actions, etc.
- Suppresses routine filings such as address changes, dividends, investor meets and trading-window notices.
- Sends only alerts scoring 6+.
- Stores seen announcement IDs in `seen.json` to reduce duplicates.

## Setup

### 1. Create a Telegram bot

In Telegram:
1. Open `@BotFather`.
2. Send `/newbot`.
3. Choose a name and username.
4. Copy the bot token.

Then open your new bot and send it `/start`.

### 2. Get your Telegram chat ID

Open this URL in Safari, replacing TOKEN with your bot token:

https://api.telegram.org/botTOKEN/getUpdates

Find `"chat":{"id":...}` and copy that number.

### 3. Create a GitHub repository

Create a new repository (a public repository is easiest for the free GitHub Actions allowance).

Upload:
- `nse_alert.py`
- `seen.json`
- `.github/workflows/nse-alert.yml`

### 4. Add GitHub secrets

Repository → Settings → Secrets and variables → Actions → New repository secret.

Add:

`TELEGRAM_BOT_TOKEN` = your BotFather token

`TELEGRAM_CHAT_ID` = your Telegram chat ID

### 5. Run it once manually

Actions → NSE High Impact News Alert → Run workflow.

If the Telegram message arrives, the system is working.

After that GitHub Actions will run every 5 minutes during the configured weekday UTC window.

## Important

This detects information only after it has been publicly disclosed through NSE. It is not an insider-information system. Always open and verify the original exchange filing before trading.

NSE itself states that company-uploaded information is displayed by the exchange without verification of the filing's adequacy, accuracy or veracity.

## Changing sensitivity

In `.github/workflows/nse-alert.yml`:

`MIN_SCORE: "6"`

Lower to `4` for more alerts; raise to `8` for fewer, more selective alerts.

`LOOKBACK_MINUTES: "15"` controls the overlap window used to reduce missed announcements caused by scheduled-job delays.
