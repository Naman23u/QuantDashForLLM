"""
Deterministic Mock Strategy for QuantDash WFA Integrity Integration Tests.
"""
import numpy as np
import pandas as pd

WARMUP_BARS = 780

def get_warmup_requirements(params=None):
    lookback = 14
    if params and 'PERIOD' in params:
        lookback = int(params['PERIOD'])
    return {
        "intraday_bars": 390,
        "daily_sessions": max(2, lookback // 7),
        "previous_close": True
    }

def run_backtest(data, params=None):
    p = {"PERIOD": 14, "STOP_MULT": 1.5, "TICK_SIZE": 0.25, "POINT_VALUE": 50.0, "COMMISSION": 5.0}
    if params:
        p.update(params)
        
    df = data.copy()
    if df.empty:
        return {"trades": [], "metrics": {}}
        
    # Lagged indicators (.shift(1))
    df['sma'] = df['close'].rolling(int(p['PERIOD'])).mean().shift(1)
    df['atr'] = (df['high'] - df['low']).rolling(int(p['PERIOD'])).mean().shift(1)
    
    trades = []
    pos = 0
    entry_price = 0.0
    entry_time = None
    stop_price = 0.0
    
    closes = df['close'].values
    highs = df['high'].values
    lows = df['low'].values
    smas = df['sma'].values
    atrs = df['atr'].values
    times = df['ts_et'].astype(str).values
    dates = df['date'].values
    
    for i in range(len(df)):
        is_last_bar_of_day = (i == len(df) - 1) or (dates[i] != dates[min(i+1, len(df)-1)])
        
        # Check exit
        if pos == 1:
            if lows[i] <= stop_price or is_last_bar_of_day:
                exit_p = stop_price if lows[i] <= stop_price else closes[i]
                pnl = (exit_p - entry_price) * p['POINT_VALUE'] - p['COMMISSION']
                trades.append({
                    "entry_time": entry_time,
                    "exit_time": times[i],
                    "direction": "long",
                    "entry_price": float(entry_price),
                    "exit_price": float(exit_p),
                    "net_pnl": float(round(pnl, 2)),
                    "commission": float(p['COMMISSION'])
                })
                pos = 0
                
        elif pos == -1:
            if highs[i] >= stop_price or is_last_bar_of_day:
                exit_p = stop_price if highs[i] >= stop_price else closes[i]
                pnl = (entry_price - exit_p) * p['POINT_VALUE'] - p['COMMISSION']
                trades.append({
                    "entry_time": entry_time,
                    "exit_time": times[i],
                    "direction": "short",
                    "entry_price": float(entry_price),
                    "exit_price": float(exit_p),
                    "net_pnl": float(round(pnl, 2)),
                    "commission": float(p['COMMISSION'])
                })
                pos = 0
                
        # Check entry
        if pos == 0 and not is_last_bar_of_day:
            if np.isnan(smas[i]) or np.isnan(atrs[i]):
                continue
            if closes[i] > smas[i]:
                pos = 1
                entry_price = closes[i]
                entry_time = times[i]
                stop_price = entry_price - (p['STOP_MULT'] * atrs[i])
            elif closes[i] < smas[i]:
                pos = -1
                entry_price = closes[i]
                entry_time = times[i]
                stop_price = entry_price + (p['STOP_MULT'] * atrs[i])
                
    return {"trades": trades, "params": p}
