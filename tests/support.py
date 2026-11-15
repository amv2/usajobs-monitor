"""Shared by the tests: configs built the way the tool builds them."""

import copy

from usajobs_monitor.config import validate_config

MINIMAL = {"search": {"locations": ["Arlington, Virginia"], "target_role_keywords": ["budget analyst"]}}


def validated(config=None):
    """``config`` laid over a minimal valid one, then run through ``validate_config``.

    Every command loads its config this way, so the code under test may rely on the
    defaults validation fills in. A test that builds a config by hand should, too.
    """
    merged = copy.deepcopy(MINIMAL)
    for section, values in (config or {}).items():
        if isinstance(values, dict):
            merged.setdefault(section, {}).update(copy.deepcopy(values))
        else:
            merged[section] = values
    return validate_config(merged)[0]
