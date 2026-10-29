# Personal AI Call Assistant

An AI that answers the calls you miss on your own mobile number. It talks in your style, records the call with a spoken disclosure, writes a speaker-labelled transcript and summary, and learns facts for next time. It can also place calls for you, and everything lives in a private dashboard you can open from your phone.

```
Caller ──► your Airtel number ──(busy / unreachable)──► forwarded to your Vobiz number
                                                                              │ answer webhook + WebSocket audio
                                                                              ▼
          Oracle Cloud VM (Mumbai) ── Caddy (HTTPS) ── FastAPI server ⇄ Gemini Live (speech-to-speech)
                                                         │   └─ records stereo WAV, live transcript
                                                         ▼
                                     Supabase Postgres ◄── worker ──► Gemini Flash (summary, actions, memory)
```

- **Live voice:** `gemini-3.8-live` with native audio, 16 kHz in and 24 kHz out. Vobiz streams exactly those rates, so no audio is converted on the live path.
- **Answers like you:** your style and rules, memory facts, the last 3 calls with this person, and caller-ID lookup in your contacts.
- **Honest:** opens with "Hi, this is <you>'s AI assistant, and this call is being recorded". It never claims to be human and never invents commitments.
- **Natural:** barge-in clears audio instantly (locally and on Vobiz's side). It hangs up by itself after saying goodbye, and auto-ends on silence. It reconnects transparently using session resumption when Gemini rotates the connection (about every 10 minutes).
- **After each call:** title, summary, key points, decisions, promises, action items, follow-ups, sentiment, a corrected transcript and new memory facts. The caller's name is saved if they said it.

## What it costs

| Piece | Cost |
|---|---|
| Gemini Live + Flash | Free tier |
| Supabase Postgres | Free (500 MB) |
| Oracle Cloud VM | Always Free |
| DuckDNS domain + Let's Encrypt HTTPS | Free |
| **Vobiz number + minutes** | **Paid.** Monthly number rental (setup fee = 1 month), plus per-minute inbound/outbound and streaming. Check current INR prices in the Vobiz console before buying. |
| Airtel call forwarding | Billed like a normal outgoing call from your plan (usually covered by unlimited plans; check yours) |

> **Privacy note:** on Gemini's free tier, Google may use inputs to improve its products. Your calls include other people's voices. For sensitive use, enable billing on the Gemini API (paid-tier data isn't used that way). See the Gemini API terms.

---

## Setup (one time)

### 1. Gemini API key
[aistudio.google.com/apikey](https://aistudio.google.com/apikey) → Create API key.

### 2. Supabase database
supabase.com → New project (region: Mumbai) → **Connect** → **Session pooler** → copy the URI and put your DB password in it. Percent-encode `@ # ? / :` and spaces in the password.

### 3. Practice on your laptop first (free)
```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env        # fill GEMINI_API_KEY, DATABASE_URL, OWNER_NAME, OWNER_PHONE
.venv\Scripts\python -m app.cli check     # verifies .env, database, Gemini models, audio devices
.venv\Scripts\python run.py               # dashboard opens at http://127.0.0.1:8765
```
Put on **headphones**, go to Live → **Start practice call**, and talk. Or use the terminal: `python -m app.cli talk`.

### 4. Server: Oracle Cloud Always Free (Mumbai)
1. Sign up at oracle.com/cloud/free. **Choose "India West (Mumbai)" as your home region.** It can't be changed later. A card is needed for verification only.
2. Compute → Instances → **Create instance**:
   - Image: **Canonical Ubuntu 24.04**
   - Shape: **Ampere VM.Standard.A1.Flex** (1 OCPU / 6 GB is plenty). If "out of capacity", use **VM.Standard.E2.1.Micro** (also Always Free).
   - **Download the private SSH key** (save it as `C:\Users\<you>\.ssh\oracle.key`).
3. Note the instance's **Public IP**.
4. Networking → your VCN → Security List → **Add Ingress Rules**: source `0.0.0.0/0`, TCP, destination ports **80** and **443**.

### 5. Free domain: DuckDNS
duckdns.org → sign in → create a subdomain (e.g. `yogi-assistant`) → set **current ip** to the VM's public IP → update ip.

### 6. Phone line: Vobiz
1. Sign up at vobiz.ai → Console → **KYC → Individual** (Aadhaar or PAN, usually approved in minutes).
2. Add balance, then **buy an Indian number**. Check the monthly price first.
3. Console → API credentials: copy **Auth ID** and **Auth Token**.
4. Console → **Voice Applications → Create**:
   - Answer URL: `https://<you>.duckdns.org/vobiz/<VOBIZ_WEBHOOK_SECRET>/answer` (method POST)
   - Hangup URL: `https://<you>.duckdns.org/vobiz/<VOBIZ_WEBHOOK_SECRET>/hangup` (method POST)
   - Attach your number to this application.

   (Dashboard → Settings → Phone line shows both URLs ready to copy once deployed.)

### 7. Deploy
```powershell
copy .env.example .env.server
# fill everything in .env.server, including PUBLIC_BASE_URL=https://<you>.duckdns.org,
# DASHBOARD_PASSWORD, SECRET_KEY, VOBIZ_* (see comments in the file for how to generate secrets)
git add -A; git commit -m "config"      # only committed code is deployed
.\deploy\deploy.ps1 -Server ubuntu@<VM-IP> -Domain <you>.duckdns.org -Key $HOME\.ssh\oracle.key
```
Open `https://<you>.duckdns.org` on your phone and log in. To update later, commit and run the same command.

### 8. Forward your Airtel calls to the assistant
On your Android: **Phone app → ⋮ → Settings → Calls/Supplementary services → Call forwarding → Voice calls**, then set:
- **Forward when busy** → your Vobiz number
- **Forward when unreachable** → your Vobiz number
- **Forward when unanswered** → OFF (so calls you just don't pick up keep ringing normally)

Leave **Always forward** OFF, or your phone never rings.

"Busy" covers: you're already on a call, **or you press Decline/Reject**. So when you can't talk, just reject and the assistant takes it.
"Unreachable" covers: switched off, no network, airplane mode.

Or dial these codes (they work on Airtel):

| What | Turn on | Turn off |
|---|---|---|
| When unanswered (keep this OFF) | | `##61#` |
| When busy | `**67*<VOBIZ_NUMBER>#` | `##67#` |
| When unreachable / switched off | `**62*<VOBIZ_NUMBER>#` | `##62#` |
| Everything off | | `##002#` |

**Test it:** call your mobile from another phone and press Reject (or turn on airplane mode). The assistant picks up and the call appears live in the dashboard.

---

## Daily use
- **Incoming:** nothing to do. Calls go to the assistant when you're busy, reject, or are unreachable, and you get a summary in History.
- **Outgoing:** Live → "Have the assistant call someone" → number + what to do → **Call now**. The other person sees your Vobiz number, not your mobile.
- **Contacts:** Memory & contacts → save names for numbers (or click "Save name" on a call). Callers who say their name are saved automatically.
- **Your rules:** Settings → *My style & rules*. Anything not allowed there, the assistant says you'll confirm.

## Commands
| | |
|---|---|
| `python run.py` | server + worker (local), opens the dashboard |
| `python -m app.cli check` | verify config, DB, Gemini models, devices |
| `python -m app.cli talk` | practice call in the terminal |
| `python -m app.migrate` | apply DB migrations |
| `python -m scripts.make_sample_call` | create a realistic sample call (Gemini TTS) to test summaries |
| `python -m pytest` | tests |
| `sudo journalctl -u assistant-server -f` | live server logs (on the VM) |
| `sudo journalctl -u assistant-worker -f` | worker logs (on the VM) |

## Troubleshooting
| Symptom | Fix |
|---|---|
| Forwarded call rings, then "number not reachable" / drops | Check that the Vobiz number is attached to the Voice Application, the Answer URL is exactly right (including the secret), and `https://<domain>/healthz` opens. Check `journalctl -u assistant-server`. |
| Call connects but silence | Usually the Answer URL returns an error: open `journalctl -u assistant-server`. Make sure Caddy has a certificate (`sudo journalctl -u caddy`): ports 80/443 must be open in **both** the Oracle Security List and the VM (setup script handles the VM). |
| Phone never forwards | Dial `*#67#` (busy) and `*#62#` (unreachable) to check forwarding status. Make sure "Always forward" is off. Some numbers need the full `+91` format. |
| Caller shows as your own number | Airtel sometimes passes the forwarding party as caller ID. The assistant then asks for the caller's name. Save contacts manually. |
| `Gemini says the Live model isn't available` | Check LIVE_MODEL against ai.google.dev/gemini-api/docs/models. Run `python -m app.cli check`. |
| `Gemini free-tier quota/rate limit hit` | Wait a minute. Long busy days may need the paid tier. |
| Database login failed / Tenant or user not found | Username must be `postgres.<project-ref>`; use the **Session pooler** string; percent-encode the password. |
| Supabase project paused | Free projects pause after a week with no activity. The worker's polling normally keeps it active; unpause in the Supabase dashboard if needed. |
| Echo / assistant interrupts itself (practice mode) | Use headphones. |
| Response feels slow | The dashboard shows *Response latency*. Lower `VAD_SILENCE_MS` (e.g. 400) for snappier turn-taking, or raise it if the assistant cuts callers off. |
| Assistant gets interrupted by line noise | Keep `VAD_START_SENSITIVITY=low`, or raise `SILENCE_RMS_THRESHOLD`. |
| Summary failed | History → call → **Re-run**. Errors are shown on the call. The worker retries 3 times automatically. |

## Next upgrades
- **"Take over" button:** transfer a live call to your real phone (Vobiz `Dial` transfer).
- **Owner briefing line:** call your own assistant number to hear today's missed calls and action items.
- **Cloned voice:** add a `SpeechRenderer` that feeds the output transcript to a voice-clone TTS (`app/voice/base.py`).
- **pgvector memory:** semantic recall of past calls instead of "last 3 + facts".
- **Notifications:** WhatsApp/Telegram summary after each call.
- **Voicemail detection** for outgoing calls (Vobiz `machine_detection`).

## Project layout
```
app/
  agent.py            call orchestrator (any transport)    transports/  vobiz.py, local.py, base.py
  voice/gemini_live.py  Live session: resumption, GoAway, VAD, end_call tool
  server.py           dashboard API, Vobiz webhooks + media WebSocket, auth
  worker.py postcall.py   job queue + Gemini analysis
  repo.py db.py migrations/   Postgres (psycopg 3 pool)
  prompts.py transcript.py audio/   prompt builder, transcript assembly, recording/resampling
  static/             dashboard (no build step)
deploy/               setup_server.sh, deploy.ps1, systemd units, Caddyfile
```
