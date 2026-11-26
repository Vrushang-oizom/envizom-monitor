# Envizom Monitor — Stage 1

Opens https://envizom.oizom.com in a headless Chrome every 60 s. If it fails to load,
shows an error page (502/503...), times out, or renders blank, it posts to Google Chat
right away. It sends one alert when the site goes down, a reminder every 30 min while it
stays down, and a 🟢 message when it recovers. A screenshot of each failure is saved in `screenshots/`.

## Create the Google Chat webhook (one time)
1. Open the Google Chat space that should receive alerts.
2. Click the space name → **Apps & integrations** → **Webhooks** → **Add webhook**.
3. Name it "Envizom Monitor" → **Save** → copy the URL.

## Run it on GitHub (works when your PC is off)
Checks every 5 minutes on GitHub's servers. Free because the repo is public; the webhook
stays hidden as a secret. (GitHub can delay scheduled runs by a few minutes when busy.)

1. **Create the repo:** github.com → **New repository** → name `envizom-monitor` → **Public** → **Create**.
2. **Upload the files:** on the repo page click **uploading an existing file**, drag in
   `monitor.py`, `requirements.txt`, `README.md`, `.gitignore` → **Commit changes**.
   **Never upload your `.env` file.**
3. **Add the workflow:** **Add file → Create new file**, type the name
   `.github/workflows/envizom-monitor.yml`, paste the contents of that file from this folder
   → **Commit changes**. (Done this way because hidden folders often don't drag-upload.)
4. **Add the webhook secret:** **Settings → Secrets and variables → Actions → New repository secret**.
   Name `GCHAT_WEBHOOK_URL`, value = your Google Chat webhook URL → **Add secret**.
5. **Test it:** **Actions** tab → **Envizom Monitor** → **Run workflow**:
   - leave "Simulate Envizom being down" unticked → run goes green ✅, no Chat message
   - tick it → run goes red ❌ and 🔴 ENVIZOM IS DOWN arrives in Chat
   - run once more unticked → 🟢 ENVIZOM IS BACK UP arrives in Chat
6. Done. From now on it runs by itself every 5 minutes.

Notes
- A red run in the Actions tab = Envizom was down at that time. Open it to see the reason; the
  failure screenshot is under **Artifacts**. GitHub may also email you about failed runs.
- To pause monitoring: Actions → Envizom Monitor → **⋯ → Disable workflow**.

## Run it on your own PC
### Install (Python 3.9+)
```bash
pip install -r requirements.txt
python -m playwright install chromium
cp .env.example .env        # Windows: copy .env.example .env
# edit .env and paste your webhook URL
```

### Test
```bash
python monitor.py --test-alert   # a test message should appear in Google Chat
python monitor.py --once         # one real check of Envizom
```

### Run
```bash
python monitor.py                # checks every 60 s until you press Ctrl+C
```
Keep it on a machine that stays on (office PC, server or VM). To start it automatically:
- **Linux server:** `nohup python3 monitor.py >> monitor.log 2>&1 &`, or a systemd service
- **Windows:** Task Scheduler → "At startup" → run `python C:\path\monitor.py`
- **Cron instead of the loop:** `* * * * * cd /path && python3 monitor.py --once >> monitor.log 2>&1`

## Exit codes (`--once`)
0 = up, 2 = down.
