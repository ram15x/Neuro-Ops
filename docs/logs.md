# Logs and alerts

## What it does

For each incident, the engine looks at the log lines from the affected instance(s) in the last 30 minutes and compares them with the day before. Lines that look the same except for numbers, ids and hosts are one group. A group is reported only if it is at least 3 times louder than usual (or never seen before), errors weigh more than warnings. The top few go into the incident, the dashboard card and the alert.

The point: a 3am alert that says "memory leak, and the new log line is `OutOfMemoryError: unable to allocate <*> bytes`, first seen 20 minutes ago" is faster to act on than a graph.

## How well does the grouping work

`python -m eval.logs_eval` downloads four 2000-line samples from Loghub (hand-labelled real logs from public systems) and scores grouping accuracy: a line counts as right only if its group has exactly the same lines as the true group.

| System | Lines | True groups | Found | Grouping accuracy |
|---|---|---|---|---|
| Apache | 2000 | 6 | 6 | 1.000 |
| HDFS | 2000 | 14 | 17 | 0.998 |
| Linux | 2000 | 118 | 110 | 0.683 |
| OpenSSH | 2000 | 27 | 23 | 0.718 |

Mean 0.85. The one setting (similarity 0.5) gives the same result anywhere from 0.3 to 0.5, so it was not tuned on these samples; above 0.6 Linux collapses. Linux and OpenSSH are the hard ones because the same message carries free text. I have not compared this against published results for the Drain paper, so I make no claim about being as good.

## What is not checked

- The simulated log lines in `backend/sim/logs.py` are written by me alongside the faults. Tests using them check that the pipeline works (the fault line shows up as new, normal traffic does not), not that real services print these lines.
- Whether the "3 times louder" cut is right for a real fleet is unknown. It is a guess that kept normal traffic out of the simulated results.
- The CloudWatch Logs reader assumes one log group with one stream per instance id (the CloudWatch agent default) and is tested with moto only.
- Two different faults that print the same line cannot be told apart by the logs.
