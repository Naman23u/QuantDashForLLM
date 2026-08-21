"""
Fixture strategy that always produces trade contract violations
for testing fail-loud WFA cycle aborts.
"""

POSITION_MODEL = "SINGLE"
MAX_OPEN_POSITIONS = 1
ALLOW_PYRAMIDING = False
ALLOW_HEDGING = False
SESSION_MODEL = "INTRADAY_FLAT"
SESSION_TIMEZONE = "America/New_York"
SESSION_CLOSE = "16:00"

def get_parameters_definition():
    return {
        "PARAM_A": {"default": 1}
    }

def get_warmup_requirements(params):
    return 100

def run_backtest(data, params):
    # Returns inverted timestamps to intentionally fail trade contract validation
    return {
        "trades": [{
            "entry_time": "2021-01-04 15:00:00",
            "exit_time": "2021-01-04 14:00:00", # Inverted!
            "direction": "long",
            "entry_price": 4000.0,
            "exit_price": 4010.0,
            "net_pnl": 200.0
        }]
    }
