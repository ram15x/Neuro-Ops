"""
Reads config/policy.yaml into a PolicyConfig, and refuses a file that is wrong
or that weakens a built-in safety rule. The point is that a typo in a
playbook should stop the app at startup, not surface at 3am as a KeyError.
"""
from datetime import timedelta

import yaml

from backend.engine.policy import ACTIONS, PLAYBOOK, PolicyConfig

KNOWN_ACTIONS = set(ACTIONS) | {"observe", "notify", "none"}
HARD_APPROVAL = {a for a, spec in ACTIONS.items() if spec["needs_approval"]}
KEYS = {"mode", "guardrails", "always_ask", "playbook"}
GUARDS = {"cooldown_minutes", "max_auto_actions_per_hour", "observe_minutes", "max_scale_out", "protected_tiers"}


class PolicyError(ValueError):
    pass


def parse(doc):
    if not isinstance(doc, dict):
        raise PolicyError("the policy file must be a mapping at the top level")
    extra = set(doc) - KEYS
    if extra:
        raise PolicyError(f"unknown top-level key(s): {', '.join(sorted(extra))}. Allowed: {', '.join(sorted(KEYS))}")
    cfg = PolicyConfig()

    mode = doc.get("mode", cfg.mode)
    if mode not in ("shadow", "approval", "auto"):
        raise PolicyError(f"mode must be shadow, approval or auto, not {mode!r}")
    cfg.mode = mode

    g = doc.get("guardrails") or {}
    bad = set(g) - GUARDS
    if bad:
        raise PolicyError(f"unknown guardrail(s): {', '.join(sorted(bad))}. Allowed: {', '.join(sorted(GUARDS))}")
    for key, attr in (("cooldown_minutes", "cooldown"), ("observe_minutes", "observe_for")):
        if key in g:
            if not isinstance(g[key], (int, float)) or g[key] < 0:
                raise PolicyError(f"{key} must be a number of minutes, 0 or more")
            setattr(cfg, attr, timedelta(minutes=g[key]))
    for key, attr in (("max_auto_actions_per_hour", "max_auto_actions_per_hour"), ("max_scale_out", "max_scale_out")):
        if key in g:
            if not isinstance(g[key], int) or isinstance(g[key], bool) or g[key] < 0:
                raise PolicyError(f"{key} must be a whole number, 0 or more")
            setattr(cfg, attr, g[key])
    if "protected_tiers" in g:
        tiers = g["protected_tiers"]
        if not isinstance(tiers, list) or not all(isinstance(t, str) for t in tiers):
            raise PolicyError("protected_tiers must be a list of tier names")
        cfg.protected_roles = tuple(tiers)

    ask = doc.get("always_ask") or []
    if not isinstance(ask, list):
        raise PolicyError("always_ask must be a list of action names")
    for a in ask:
        if a not in ACTIONS:
            raise PolicyError(f"always_ask: {a!r} is not an action that can be run ({', '.join(sorted(ACTIONS))})")
    cfg.extra_approval = tuple(ask)

    if "playbook" in doc:
        book = doc["playbook"]
        if not isinstance(book, dict):
            raise PolicyError("playbook must map each cause to a list of actions")
        merged = dict(PLAYBOOK)
        for cause, ladder in book.items():
            if cause not in PLAYBOOK:
                raise PolicyError(f"playbook: unknown cause {cause!r}. Known: {', '.join(sorted(PLAYBOOK))}")
            if not isinstance(ladder, list) or not ladder:
                raise PolicyError(f"playbook.{cause} must be a non-empty list")
            for a in ladder:
                if a not in KNOWN_ACTIONS:
                    raise PolicyError(f"playbook.{cause}: unknown action {a!r}. Known: {', '.join(sorted(KNOWN_ACTIONS))}")
            if ladder[-1] not in ("notify", "none"):
                raise PolicyError(f"playbook.{cause} has to end with notify (or none), or a person is never told when the fixes run out")
            merged[cause] = ladder
        cfg.playbook = merged
    return cfg


def load(path):
    with open(path, encoding="utf-8") as f:
        return parse(yaml.safe_load(f))
