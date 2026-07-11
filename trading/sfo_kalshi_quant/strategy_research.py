"""Compatibility facade for the Strategy Lab artifact builder.

Implementation lives in :mod:`sfo_kalshi_quant.strategy_lab`; imports from
this historical module remain stable for operational scripts and callers.
"""

from datetime import UTC

from . import strategy_lab as _strategy_lab
from ._util import (
    _date_from_string,
    _db_table_exists,
    _env_float,
    _load_json_optional,
    _null_metric,
    _round,
    _round_dict,
    _table_exists as _sqlite_table_exists,
    _to_float,
)
from .cities import CITIES
from .config import DEFAULT_DB_PATH, DEFAULT_FORECASTER_ROOT, SFO_TZ
from .dataset_research import DEFAULT_MIN_AFTER_COST_TRADES, DEFAULT_MIN_MATCHED_ROWS
from .exits import (
    DEFAULT_NO_STOP_LOSS_PCT,
    DEFAULT_NO_TAKE_PROFIT_PCT,
    DEFAULT_RESEARCH_NO_SETTLEMENT_FIRST_MIN_COST,
    DEFAULT_STOP_LOSS_PCT,
    DEFAULT_TAKE_PROFIT_PCT,
    DEFAULT_YES_STOP_LOSS_PCT,
    DEFAULT_YES_TAKE_PROFIT_PCT,
)
from .settlement_truth import is_pre_resolution_decision as _is_strategy_pre_resolution
from .strategy_lab import (
    build,
    calibration,
    consensus_offline,
    dataset_summary,
    forecast_health,
    paper_card,
    profiles,
    readiness,
    status_alerts,
)

_DOMAIN_MODULES = (
    build,
    profiles,
    calibration,
    readiness,
    paper_card,
    forecast_health,
    dataset_summary,
    status_alerts,
    consensus_offline,
)
for _module in _DOMAIN_MODULES:
    globals().update(
        {
            name: value
            for name, value in vars(_module).items()
            if not name.startswith("__") and (name.startswith("_") or callable(value))
        }
    )

for _name, _value in vars(_strategy_lab).items():
    if _name.isupper():
        globals()[_name] = _value

del _module, _name, _value
