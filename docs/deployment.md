# Deployment

Three independently deployed pieces, all on free tiers.

```
   Evaluator's browser
          │
          ▼
   Streamlit Community Cloud  ──── server-side HTTPS + X-API-Key ────▶  Render (FastAPI)
   (dashboard/app.py, public URL)   no CORS involved, no DB access           │
                                                                             │ MONGODB_URI
                                                                             ▼
                                                                 MongoDB Atlas M0 (free)
```

**The critical property: only the API process holds `MONGODB_URI`.** The dashboard's environment
does not contain it and `dashboard/requirements.txt` installs no database driver, so the
presentation-only boundary is a deployment fact rather than a coding convention. Even a mistaken
`import motor` in the dashboard would have nothing to connect to.

## 1. Database — MongoDB Atlas M0

1. Create a free M0 cluster.
2. Create a database user with **read/write on the `smartdialer` database only** — not an admin
   user. Least privilege matters even for a prototype.
3. Under Network Access, allow the API host's egress (Render's free tier has no static IP, so
   `0.0.0.0/0` is the practical option; note this in your own risk assessment).
4. Copy the connection string. **Never commit it.**

## 2. Backend — Render web service

`render.yaml` is committed and declares the build and start commands. Create the service from the
repository, then set these environment variables in the Render dashboard:

| Variable | Purpose | Set where |
|---|---|---|
| `MONGODB_URI` | Atlas connection string | Render dashboard (secret) |
| `MONGODB_DB_NAME` | Database name, `smartdialer`. Simulations use `smartdialer_simulation` | render.yaml |
| `DECISION_RETENTION_MINUTES` | TTL on pacing/safety decision records, `1440` | render.yaml |
| `API_KEY` | Shared secret for mutating endpoints | Render dashboard (secret) |
| `MONGO_MAX_POOL_SIZE` | Motor pool size, `20` — Atlas M0 caps connections | render.yaml |
| `LOG_LEVEL` | `INFO` in production, so borrower data volume stays low | render.yaml |
| `DIALER_ENABLED` | `true` to run the dialer loop | render.yaml |
| `RECOVERY_ENABLED` | `true` to run the recovery sweeps | render.yaml |
| `CORS_ORIGINS` | Restricted list for direct browser use of `/docs` | render.yaml |

After the first deploy, confirm the background workers actually run by checking the logs for
`startup_dialer_started` and `startup_recovery_started`. **Free instances sleep when idle**, which
pauses both loops; the README says so plainly rather than claiming always-on operation.

## 3. Dashboard — Streamlit Community Cloud

1. Connect the same GitHub repository.
2. Set **Main file path** to `dashboard/app.py`.
3. Set **Requirements file** to `dashboard/requirements.txt`. Getting this wrong installs the
   backend's dependencies into the dashboard and quietly violates the boundary above.
4. In the Community Cloud secrets manager (which populates `st.secrets`), set:

   ```toml
   SD_API_BASE_URL = "https://your-api.onrender.com"
   SD_API_KEY = "the same value as the API's API_KEY"
   ```

   **Do not set `MONGODB_URI` here.**
5. Confirm the deployed Python version suits `streamlit` and `pandas`.

`dashboard/.streamlit/secrets.toml.example` documents the names only and is committed;
`secrets.toml` itself is git-ignored.

Alternative if Community Cloud is unsuitable: a second Render web service with start command
`streamlit run dashboard/app.py --server.port $PORT --server.address 0.0.0.0`. Document whichever
you use.

## 4. CORS — and why it is not what makes the dashboard work

The dashboard calls the API from the **Streamlit server**, not from the browser, so those requests
are not subject to CORS at all. `CORS_ORIGINS` exists only for direct browser access to `/docs`
and for any future browser client.

Do not add the Streamlit app's URL to `CORS_ORIGINS` believing it is required, and do not widen
CORS while debugging a dashboard problem — the cause will be somewhere else. This is
counter-intuitive enough that it is worth re-reading before touching the setting.

## 5. Production hardening

- API key required on every mutating endpoint. This is genuinely effective here because the key
  lives in Streamlit's server-side secrets and never reaches a browser.
- Per-IP cooldown on fault-injection endpoints so a public demo cannot be trivially disrupted.
- `LOG_LEVEL=INFO`, and the pymongo/motor loggers are pinned to `WARNING` so the driver cannot
  dump command payloads containing borrower data.
- Phone numbers are redacted to their last four digits everywhere they are logged.
- No stack traces in responses; unexpected errors return a correlation id that also appears in the
  logs.
- Streamlit's XSRF and CORS protections are left enabled in `dashboard/.streamlit/config.toml`.

## 6. Seeding the deployed demo

Either call the protected endpoint:

```bash
curl -X POST "$API/api/campaigns" -H "X-API-Key: $API_KEY" \
     -H "Content-Type: application/json" -d '{"name":"Demo Collections Campaign"}'
curl -X POST "$API/api/campaigns/$CAMPAIGN_ID/seed" -H "X-API-Key: $API_KEY" \
     -H "Content-Type: application/json" -d '{"agents":10,"borrowers":300}'
```

or run the script once against the production database:

```bash
python -m scripts.seed_production --agents 10 --borrowers 300 --start
```

The dashboard also exposes **Seed demo data** under the Overview tab, so an evaluator needs no
shell access. Agents are created `OFFLINE`; log them in from the Agents tab or the agents API.

## 7. Post-deploy smoke checklist

- [ ] `GET /health` returns `database: connected`.
- [ ] `/docs` renders the OpenAPI page.
- [ ] The dashboard loads at its public URL and shows real data.
- [ ] A campaign starts and calls appear in the Calls tab.
- [ ] A simulation runs to completion from the Simulation tab.
- [ ] Fault injection works and the panels visibly react.
- [ ] Streamlit logs show successful API calls, not fallbacks.
- [ ] The dashboard environment contains **no** `MONGODB_URI`.
- [ ] A mutating request without `X-API-Key` returns 401.

## 8. Known free-tier limitations

- **Two independent cold starts.** The dashboard may wake before the API; that is exactly what its
  "backend is waking up" message exists for. First load can take ~30 s.
- **Instances sleep when idle**, which pauses the dialer and recovery loops. Dialing resumes on the
  next request that wakes the service.
- **Atlas M0 has a low connection cap and shared CPU.** `MONGO_MAX_POOL_SIZE` is set to 20 for this
  reason. Only the API consumes database connections; the dashboard adds HTTP load only.
- **A single deployed instance runs one dialer worker.** The multi-worker concurrency guarantees are
  proven by the test suite and the simulation harness, not by production traffic.
- **Each dashboard viewer is an independent Streamlit session** issuing its own refresh requests, so
  API read load scales with concurrent viewers. Negligible at demo scale.
