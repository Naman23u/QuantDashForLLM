"""
Rogue Straddling Strategy Fixture.
Emits a trade that enters during warmup and remains open into the evaluation window.
Used to verify that worker_engine detects and rejects boundary straddling violations.
"""
def get_warmup_requirements(params=None):
    return {"intraday_bars": 500}

def run_backtest(data, params=None):
    # Emits a rogue trade entered on 2020-06-30 (warmup) that exits on 2020-07-02 (OOS)
    return {
        "trades": [
            {
                "entry_time": "2020-06-30 14:00:00",
                "exit_time": "2020-07-02 10:30:00",
                "direction": "long",
                "entry_price": 3100.0,
                "exit_price": 3120.0,
                "net_pnl": 1000.0,
                "commission": 5.0
            }
        ],
        "params": params or {}
    }
