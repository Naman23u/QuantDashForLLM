"""
Fixture strategy with malformed contract metadata for testing pre-flight and API rejection.
"""

POSITION_MODEL = "SINGLE"
MAX_OPEN_POSITIONS = 3 # Contradiction with SINGLE!
ALLOW_PYRAMIDING = "false" # String instead of boolean!
ALLOW_HEDGING = False
SESSION_MODEL = "INTRADAY_FLAT"
SESSION_TIMEZONE = "America/New_York"
SESSION_CLOSE = "banana" # Invalid close time!

def run_backtest(data, params):
    return {"trades": []}
