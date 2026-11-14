# Turbofan experiment (not part of the product)

This is the original failure and remaining-useful-life work from v2. It is kept
for the record and nothing in `backend/` imports it.

Why it was pulled out:

- The models were trained on NASA's turbofan engine data (`data/`). A jet engine
  wears out, an EC2 instance does not, so "remaining useful life" has no real
  meaning for a cloud server. The accuracy measured on turbofan data says
  nothing about EC2.
- The later "real data" models (`train_rf_model.py`, `train_xgb_model.py`,
  `auto_convert_and_train.py`) used a label defined as `cpu > 80`. CPU was also
  an input, so the models were learning a rule they had been handed. Their
  accuracy numbers are not meaningful.

What replaced it: `backend/detect/forecast.py`, a time-to-breach forecast for
disk, memory and burst credits, scored in `docs/scorecard.md`.

The scripts here may not run as they are, their imports point at the old layout.
