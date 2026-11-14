# Architecture

## The loop

Every 5 minutes of data, the engine does this for each instance:

1. Pull new datapoints from a source: the simulated fleet, or CloudWatch (not wired in yet).
2. Score each metric. Detectors only look backwards, so scoring never uses future data.
3. Turn scores into signals: an anomaly signal when a score stays over its threshold for a few points in a row, and a forecast signal when a ramp will hit a limit soon.
4. Group signals into an incident with a cause, and attach the log clusters that are new or louder (see logs.md). Peers matter: if the whole tier moves together it is load, if one box moves it is that box.
5. Ask the policy what to do. The policy walks a ladder of actions for that cause and stops at the first one the guardrails allow.
6. Run the action through an executor, then wait and check whether the signal cleared. If not, move one step up the ladder.
7. When the trouble ends, undo temporary changes and close the incident.

8. If a person is needed (no safe fix left, an approval is waiting, or a critical incident in shadow mode), send one alert per incident and kind. Send one more when it resolves.

Every step is written to the audit log with its reason, including each alert.

## Policy

Guardrails and ladders are read from `config/policy.yaml` (`backend/engine/policy_file.py`). The loader refuses unknown keys, unknown actions, a ladder that does not end in notify, and anything that would remove the built-in approval rule for moving a host or switching credit mode.

- Modes: shadow (record only), approval (a person answers every request), auto.
- Ladders per cause, for example memory leak: restart the service, then reboot, then tell a person.
- Guardrails: 30 minute cooldown per target, at most 4 automatic actions an hour, the `db` tier is protected, scale-out is capped at 2 extra instances, and the engine watches a new incident for 30 minutes before acting on CPU contention.
- Always needs a person: moving a host (downtime) and switching to unlimited credits (cost).

## Detectors

Eight are compared in `docs/scorecard.md`. The default, `hybrid_seasonal`, averages three scores: a rolling z-score, an Isolation Forest, and a time-of-day band. It was picked on the scorecard, not by intuition. Thresholds are calibrated on simulator seeds 1 to 3, and scored on others.

## Alerts

`backend/engine/notify.py`. The message says what, where, for how long, why a person is being told, what the engine already tried and whether it worked, the log clusters, and a first thing to check for that cause. Channels: SNS, a Slack webhook, and a recorder (the simulated demo uses only the recorder). Rules: one message per incident per kind, at most 10 an hour (the rest are counted in the next message), and a dead channel never stops the engine.

## Reports and metrics

`backend/engine/report.py` derives per-incident rows (time open, time to first action, fixed by the engine, handed to a person), a summary, and a markdown postmortem draft, all from the audit log. `backend/engine/metrics.py` renders Prometheus text for `/metrics`.

## Storage

The audit log goes to the database as it happens, and optionally to S3 (gzipped JSON lines, one file per run per day, encrypted, versioned bucket with a lifecycle rule in Terraform).

## Executors

- `SimExecutor` changes the simulated fleet, so later metrics respond to the action (this is what makes the closed loop test possible).
- `AwsExecutor` uses SSM Run Command for on-box work (no SSH), EC2 for reboot and stop/start, and Auto Scaling for scale-out. Checked with moto only.

## API and dashboard

`/api/v1/engine/*` exposes status, fleet, series, incidents, audit, history (the stored audit log), approvals and mode. Approvals and mode changes need a login. The dashboard reads the same snapshot shape as the recordings, so the hosted demo and the live view use one drawing path.

## Deployment

`infra/terraform` creates one instance with a role scoped to resources tagged `neuroops:managed=true`. It has never been applied.
