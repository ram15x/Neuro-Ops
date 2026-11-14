from backend.data.loaders import load_nab, load_sim, load_real_ec2
from eval.scorecard import evaluate, check, DEFAULT_DETECTOR, MAIN_BUDGET


def test_loaders_give_one_shape():
    nab, sim, real = load_nab(), load_sim(days=6, density=1), load_real_ec2()
    assert len(nab) == 10
    assert all(s.source == "nab" and not s.simulated for s in nab)
    assert all(s.simulated for s in sim)
    assert not any(s.kind == "credits" for s in sim)
    for s in nab + sim + real:
        assert list(s.frame.columns) == ["timestamp", "value"]
    # the old cpu > 80 label is not carried over as truth, only stress runs
    assert real[0].windows


def test_default_detector_holds_up_against_a_static_threshold():
    # two seeds, because one simulated fortnight is too few faults to judge on
    series = load_sim(seed=7) + load_sim(seed=8)
    res = evaluate(series, budgets=(MAIN_BUDGET,), detectors=[DEFAULT_DETECTOR, "static_threshold"])
    best, static = res[(MAIN_BUDGET, DEFAULT_DETECTOR)], res[(MAIN_BUDGET, "static_threshold")]
    assert best["recall"] >= 0.7
    assert best["recall"] >= static["recall"]
    assert best["false_per_day"] <= MAIN_BUDGET


def test_check_reports_a_drop():
    fake = {
        "nab": {f"{MAIN_BUDGET}|{DEFAULT_DETECTOR}": {"recall": 0.1}},
        "sim": {f"{MAIN_BUDGET}|{DEFAULT_DETECTOR}": {"recall": 0.9}},
        "forecast": {"kinds": {"disk_fill": {"warned": 1, "events": 2}}, "false_warnings_per_series_day": 0.0},
    }
    problems = check(fake)
    assert len(problems) == 2


def test_check_catches_shadow_mode_acting():
    ok = {"recall": 0.9}
    loop = dict(faults=10, diagnosed=10, breaches=0, breaches_possible=4, wrong_actions=0, actions=5)
    fake = {
        "nab": {f"{MAIN_BUDGET}|{DEFAULT_DETECTOR}": ok}, "sim": {f"{MAIN_BUDGET}|{DEFAULT_DETECTOR}": ok},
        "forecast": {"kinds": {}, "false_warnings_per_series_day": 0.0},
        "closed_loop": {"clean": {"auto": loop, "shadow": dict(loop, actions=1)}},
    }
    assert check(fake) == ["closed loop: shadow mode executed something"]
