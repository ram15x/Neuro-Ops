# Model card

## What is a model here

The default path has no pre-trained model. Detection averages a rolling z-score, an Isolation Forest and a time-of-day band, forecasting is a robust line fit (Theil-Sen), and diagnosis is rules. The Isolation Forest part is fitted per series at run time, not trained ahead.

## Approaches that were rejected

- The `is_anomaly` label was `cpu > 80`. A Random Forest or XGBoost trained on it, with CPU as an input, learns the rule back. Its high accuracy meant nothing.
- A NASA turbofan remaining-useful-life model has no meaning for an EC2 instance, so it is not used.
- The "service correlation" feature invented four services that did not exist.
- The training data was one instance, about 14 hours of points over 2.3 days. Memory and disk came from my own psutil agent, because EC2 does not report them without the CloudWatch agent.
- Log analysis used a free OpenRouter model. I could not verify its availability from the build environment.

## How the current detectors are evaluated

- Real data: the NAB AWS CloudWatch CPU series with labelled anomaly windows (16 windows).
- Simulated data: a 7-instance fleet with injected faults, 3 seeds for the detector table.
- Thresholds are tuned on half of each series and scored on the other half. Alerts within an hour count as one episode. Results are reported at fixed false-alarm budgets.

## Log grouping

Not a trained model: a small version of the Drain method. See logs.md for the check on real labelled logs. It does not read meaning, so two different problems that print the same line look the same.

## Known limits

- Recall on real data is about half of labelled windows at 0.25 false alarms per series-day.
- Slow ramps are hard for anomaly detectors, which is why the forecast exists.
- A weak network drop is the hardest fault in hard mode (2 of 6 noticed).
- The simulator is the author's model of AWS behaviour. T3 burst credits are modelled, but real noisy-neighbour effects, deploy patterns and traffic shapes are not.
- Nothing has been run against a live AWS account.
