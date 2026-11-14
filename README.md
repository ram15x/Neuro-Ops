# NeuroOps

A self-healing and alerting layer for a small AWS fleet. It watches CloudWatch-style metrics, works out what kind of trouble a server is in, and fixes the safe things itself: restart a service, clean a disk, add an instance. It checks that the fix worked, undoes temporary changes, and writes down every decision with the reason. When it cannot fix something, it tells a person: what broke, since when, what it already tried, which log lines are new, and what to check first.

I built the first version in March 2026 on the AWS free tier to learn. This version is a rebuild: honest evaluation, a simulated fleet so it can be demoed without an AWS bill, and guardrails so it is safe to leave running.

**Demo:** https://ram15x.github.io/NeuroOps/ (recorded runs on a simulated 7-server fleet, no backend needed)

## Why not just use what AWS has

AWS already has CloudWatch anomaly detection, DevOps Guru, predictive scaling, and EventBridge with SSM Automation. If you live fully inside AWS and are happy with those, use them. What this adds:

- A policy layer between "something looks wrong" and "do something": a ladder of fixes per fault type, cooldowns, an hourly action cap, protected tiers (the database is never touched automatically), actions that always need a person, and shadow mode as the default.
- Verification and rollback: after an action it checks the signal went away, tries the next step if not, and puts temporary changes (extra instances, credit mode) back when the trouble ends.
- A scorecard for the detectors themselves, on real data, so the choice of detector is measured and not assumed.
- An alert written for the person on call, with the log lines that are new or louder than usual, grouped so 4000 lines read as a handful of kinds of line.
- It runs anywhere, with a simulator, with no AWS account.

## How it works

```
metrics -> signals -> diagnosis -> policy -> executor -> verify
(sim or     anomaly,   cause of    guardrails  simulated   did it
CloudWatch) forecast   the trouble  + mode     or AWS      work?
                                  audit log of every step
```

- Signals: an anomaly score (the default detector, `hybrid_seasonal`, averages a z-score, an Isolation Forest and a time-of-day band) plus a time-to-breach forecast for slow ramps like disks and memory leaks.
- Logs: lines are grouped by shape (numbers, ids and hosts masked), and the clusters that are new or much louder than the day before are attached to the incident. See [docs/logs.md](docs/logs.md).
- Changes: if a deploy shows up in the logs in the two hours before an incident, the alert and the card say so (it only works if your service logs its releases).
- Reports: `/api/v1/engine/report` gives time open, time to first action and how many incidents the engine fixed by itself. `/api/v1/engine/incidents/{id}/postmortem` writes a markdown draft from the audit log, with the root-cause section left for a person.
- Policy: guardrails and fix ladders live in [config/policy.yaml](config/policy.yaml). A wrong file stops the app at startup with the reason, and you can add approval requirements but cannot remove the built-in ones. If the file and `ENGINE_MODE` disagree, the safer mode wins.
- Metrics: `/metrics` serves Prometheus text (incidents, actions, checks, alerts, mode, tick time) so you can watch the engine with Grafana.
- Alerts: when the engine runs out of safe fixes, needs an approval, or sees a critical incident in shadow mode, it sends one message per incident (SNS, Slack, or a recorder in the demo) and one more when it resolves. An hourly cap keeps it from becoming noise.
- Diagnosis: plain rules. A CPU rise across a whole tier is load, on one box it is a runaway process, and so on.
- Policy: three modes. Shadow records what it would do. Approval waits for a person on every action. Auto runs what the guardrails allow and asks for the rest.
- Details are in [docs/architecture.md](docs/architecture.md).

## Run it

```
pip install -r requirements.txt
python -m eval.export_replay          # builds the demo recordings
uvicorn backend.main:app --reload     # API, plus the dashboard at /dashboard/
```

Or `docker compose up`. Nothing else is needed: it uses sqlite and an in-memory Redis. API docs are at `/docs`.

CI runs lint, tests, the scorecard, a Docker build that boots and serves the dashboard, and `terraform validate`. Pushes publish an image to ghcr.io (`.github/workflows/release.yml`). That only publishes an image, nothing deploys to AWS by itself.

Tests: `pip install -r requirements-dev.txt && pytest`. The detector scorecard: `python -m eval.scorecard` (about 7 minutes).

## What the numbers say

Full tables are in [docs/scorecard.md](docs/scorecard.md). The short version:

- On real AWS CPU series from the NAB benchmark, the default detector catches 8 of 16 labelled anomaly windows at about 0.2 false alarms per series-day. A static threshold catches 3. That is modest. It is half, not nearly all.
- On the simulated fleet the closed loop looks very good (no ramp reaches the danger line, 48 of 48 faults diagnosed). Do not read much into that. The simulator and the diagnosis rules were written by the same person, and the simulator decides which action fixes which fault, so this shows the wiring works.
- Hard mode (weaker, noisier, overlapping faults) is the fairer read: 35 of 54 faults diagnosed correctly, 1 of 18 ramps reached the danger line, and 6 actions were taken with no real fault behind them.
- The forecast warned on all 18 injected ramps about 4 to 6 hours ahead, with no false warnings on quiet time.

## What is not proven

- The AWS executors and the CloudWatch source are tested against moto and botocore stubs only. They have never touched a real account.
- The CloudWatch metrics source and the CloudWatch Logs source exist but are not wired into the running loop. The running engine uses the simulated fleet, and its alerts are only recorded, not sent, unless you set `ALERT_SEND_SIMULATED=true`.
- The log lines in the simulator are written by me, so tests on them check the mechanics. The grouping itself was checked on real labelled logs (see docs/logs.md).
- Alerts reach SNS and Slack only through code tested with moto and a stub. I have not sent a real email.
- The audit log is stored in the database, and optionally copied to S3 once a simulated day (`ARCHIVE_S3_BUCKET`, tested with moto only). That is all that is stored. Open incidents are not restored after a restart.
- The real EC2 capture I made in March (`data/real_training_data.csv`) is about 14 hours of points on one instance, too short to score.
- The old v2 endpoints are still in `backend/api/` but are off unless `LEGACY_API_ENABLED=true`. Some were built on a `cpu > 80` label, which is circular. See [docs/model-card.md](docs/model-card.md).
- `infra/terraform` has never been applied. CI only validates it.
- The postmortem draft knows what happened and when, not why.
- `/metrics` has no login. It holds counts only, but put it behind your network rules.
- Auth is a single admin user. Fine for a demo, not for a team.

## Layout

```
backend/engine   signals, diagnosis, policy, executors, the loop
backend/detect   detectors and the forecast
backend/logs     log grouping (Drain-style) and "what is new" ranking
backend/storage  S3 archive of the audit log
backend/sim      the simulated AWS fleet and its logs
backend/api      FastAPI routes (engine, auth, health)
dashboard        static dashboard, plays recordings or talks to the live API
eval             scorecard, calibration, closed-loop test, demo recordings
infra/terraform  deployment, validate-only
research         the old NASA turbofan RUL work, kept with a note on why it does not apply
legacy           v2 deploy files and scripts
```

## Keys and secrets

Nothing in the demo needs an AWS key. For a real deployment, see `infra/terraform/README.md`. The old keys in my local `.env` are dead and the file is gitignored.
