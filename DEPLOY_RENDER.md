# Deploying to Render (Free Tier)

## 1. Push to GitHub
Do **not** commit `credentials.json` — add it to `.gitignore`. Its contents go into
Render as an environment variable instead (step 3).

```
git init
git add .
git commit -m "Attendance app"
git branch -M main
git remote add origin https://github.com/<you>/<repo>.git
git push -u origin main
```

## 2. Create the Render service
- New → Web Service → connect your GitHub repo
- Render will detect `render.yaml` and pre-fill most settings, or set manually:
  - Build Command: `pip install -r requirements.txt`
  - Start Command: `python app_direct.py`
  - Plan: Free

## 3. Set environment variables (Render dashboard → Environment)
| Key | Value |
|---|---|
| `GOOGLE_CREDENTIALS_JSON` | Paste the entire contents of your `credentials.json` file |
| `SPREADSHEET_NAME` | `Attendance Course EE30` (or your actual sheet name) |
| `WORKSHEET_NAME` | `Sheet1` |
| `ADMIN_PASSWORD` | Your own password for `/admin/reset` and `/` |
| `APP_SECRET_KEY` | Any random string (or let Render auto-generate, see render.yaml) |
| `CAMPUS_IP_RANGES` | Leave blank for now, or your campus's **public** IP/CIDR |

You do NOT need to set `APP_BASE_URL` or `PORT` manually — Render provides
`RENDER_EXTERNAL_URL` and `PORT` automatically, and the app now reads those.

## 4. Free-tier caveats — please read
- **Cold starts:** the free instance sleeps after ~15 min of no traffic and
  takes 20–50s to wake up. Your QR token is only valid 5 seconds, so the
  very first scan of class will fail if the app is asleep. **Visit the app's
  URL yourself 1–2 minutes before class starts** to wake it up, or set up a
  free pinger (see below).
- **Optional keep-alive:** point a free service like UptimeRobot or
  cron-job.org at `https://yourapp.onrender.com/healthz` every 10 minutes
  during school hours. This reduces (doesn't eliminate) sleep during gaps
  between classes.
- **Ephemeral disk:** local files (`attendance_log.csv`, `fingerprints.log`,
  saved browser sessions) are wiped whenever the instance restarts or
  redeploys. Google Sheets stays intact regardless — that remains your real
  record. The only real risk is a handful of just-submitted rows that
  hadn't yet been flushed to Sheets (worst case ~15 seconds' worth) if a
  restart happens at that exact moment mid-class, which is unlikely since
  the instance won't be idle-sleeping while actively receiving scans.
- **Campus IP restriction:** if you turn `CAMPUS_IP_RANGES` on, use your
  campus network's **public** IP (what a site like whatismyip.com shows
  from inside your building), not an internal `10.x`/`192.168.x` address —
  those only exist on the local LAN and mean nothing to a server on the
  internet.

## 5. Update the QR base URL
Once deployed, Render gives you a URL like `https://simple-attendance.onrender.com`.
The app auto-detects this via `RENDER_EXTERNAL_URL`, so you shouldn't need to
set `APP_BASE_URL` manually — but you can override it if you attach a custom domain.
