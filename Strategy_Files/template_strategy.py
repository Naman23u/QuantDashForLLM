"""
===============================================================================
QuantDash Canonical Strategy Template
===============================================================================
Every strategy plugged into QuantDash MUST adhere to this exact contract.

Functions Required:
1. round_to_tick(price, tick_size=0.25)
2. run_backtest(data, params) -> dict
3. run_data_permutation(data, params, n_permutations=1000, seed=42) -> dict (optional but recommended)

Data Contract:
- `data`: Either a file path (.parquet, .csv) OR a pre-filtered pandas DataFrame.
- Required DataFrame Columns:
    - ts_et: datetime64[ns, America/New_York]
    - open, high, low, close, volume: float
    - date: datetime.date (ET calendar date)
===============================================================================
"""

import os
import sys
import numpy as np
import pandas as pd
from datetime import datetime, timezone
import quant_metrics

WARMUP_BARS = 1500

# Strategy Position & Session Contract
POSITION_MODEL = "SINGLE"
MAX_OPEN_POSITIONS = 1
ALLOW_PYRAMIDING = False
ALLOW_HEDGING = False
SESSION_MODEL = "INTRADAY_FLAT"
SESSION_TIMEZONE = "America/New_York"
SESSION_CLOSE = "16:00"


def get_warmup_requirements(params: dict = None) -> dict:
    """
    QuantDash Dynamic Multi-Tiered Warmup Contract:
    Exposes exact historical lookback requirements needed to warm up indicators.
    
    Supported keys:
      - 'intraday_bars': Number of 1-minute intraday bars (e.g. 500)
      - 'daily_sessions': Number of full daily trading sessions (e.g. 20 -> 20 * 390 bars)
      - 'previous_close': If True, adds +1 session buffer (+390 bars) for prior-day high/low/close
    """
    slow_p = 30
    if params:
        slow_p = max(int(params.get('FAST_PERIOD', 10)), int(params.get('SLOW_PERIOD', 30)), 14)
    
    return {
        "intraday_bars": max(500, slow_p * 20),
        "daily_sessions": max(5, slow_p // 5),
        "previous_close": True
    }


def round_to_tick(val: float, tick_size: float = 0.25) -> float:
    """Rounds price to the nearest valid futures tick."""
    if tick_size <= 0:
        return float(val)
    return float(np.round(val / tick_size) * tick_size)


def prepare_data(data) -> pd.DataFrame:
    """
    Standard data preparation hook.
    Ensures correct datetime localization, sorting, and bar indicators.
    """
    if isinstance(data, str):
        if not os.path.exists(data):
            raise FileNotFoundError(f"Data file not found: {data}")
        df = pd.read_parquet(data) if data.endswith('.parquet') else pd.read_csv(data)
    else:
        df = data.copy()

    if df.empty:
        return df

    # Standardize column casing
    df.columns = [c.lower() for c in df.columns]

    # Timestamp conversion to US Eastern Time (ET)
    if 'ts_et' not in df.columns:
        if 'ts_event' in df.columns:
            ts_col = pd.to_datetime(df['ts_event'])
        elif 'timestamp' in df.columns:
            ts_col = pd.to_datetime(df['timestamp'])
        else:
            ts_col = pd.to_datetime(df.index)
        
        if ts_col.dt.tz is None:
            df['ts_et'] = ts_col.dt.tz_localize('UTC').dt.tz_convert('America/New_York')
        else:
            df['ts_et'] = ts_col.dt.tz_convert('America/New_York')
    
    if 'date' not in df.columns:
        df['date'] = df['ts_et'].dt.date

    df = df.sort_values('ts_et').reset_index(drop=True)
    return df

def get_default_parameters() -> dict:
    """Returns canonical default parameters for QuantDash UI and optimization."""
    return {
        # Strategy specific
        "FAST_PERIOD": 10,
        "SLOW_PERIOD": 30,
        "STOP_ATR_MULT": 2.0,
        
        # Execution & Sizing
        "TICK_SIZE": 0.25,
        "SLIPPAGE_TICKS": 1,
        "MINI_POINT_VALUE": 50.0,
        "MICRO_POINT_VALUE": 5.0,
        "COMMISSION_MINI_RT": 5.76,
        "COMMISSION_MICRO_RT": 1.82,
        "RISK_TYPE": "$",
        "RISK_VALUE": 1000.0,
        "RISK_FREE_RATE_PCT": 4.0,
        "MAX_TRADES_PER_DAY": 5
    }


def run_backtest(data, params: dict = None) -> dict:
    """
    Core backtest execution function called by QuantDash.
    
    Parameters:
    - data: Filepath (str) or pandas DataFrame containing 1-minute OHLCV bars.
    - params: Dictionary of optimizable strategy parameters.
    
    Returns:
    - dict with keys:
        - "trades": list of trade dictionaries
        - "metrics": quant_metrics summary stats dictionary
        - "equity_curve": list of equity points (optional)
    """
    # 1. Default Parameters
    default_params = get_default_parameters()
    
    p = default_params.copy()
    if params:
        p.update(params)

    df = prepare_data(data)
    if df.empty:
        return {"trades": [], "metrics": {}}

    # 2. Compute Lagged Indicators (Strictly Avoid Lookahead Bias)
    # Rules: All indicators must be shifted by 1 bar before entry decision!
    df['fast_ma'] = df['close'].rolling(int(p['FAST_PERIOD'])).mean().shift(1)
    df['slow_ma'] = df['close'].rolling(int(p['SLOW_PERIOD'])).mean().shift(1)
    
    # ATR for dynamic stops (Lagged)
    high_low = df['high'] - df['low']
    high_close = (df['high'] - df['close'].shift(1)).abs()
    low_close = (df['low'] - df['close'].shift(1)).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df['atr'] = tr.rolling(14).mean().shift(1)

    # 3. Simulate Trades Bar-by-Bar (Session-Aware)
    trades = []
    current_equity = 100000.0
    starting_capital = 100000.0
    slippage_pts = p['SLIPPAGE_TICKS'] * p['TICK_SIZE']
    tick_size = p['TICK_SIZE']

    # Group by trading day
    grouped = df.groupby('date')
    
    for date, day_df in grouped:
        highs = day_df['high'].values
        lows = day_df['low'].values
        closes = day_df['close'].values
        fast_mas = day_df['fast_ma'].values
        slow_mas = day_df['slow_ma'].values
        atrs = day_df['atr'].values
        times_et = day_df['ts_et'].values
        times_dt = pd.to_datetime(day_df['ts_et'])
        times_h = times_dt.dt.hour.values
        times_m = times_dt.dt.minute.values
        time_strs = day_df['ts_et'].astype(str).values



        position = 0 # 1 for Long, -1 for Short, 0 Flat
        entry_price = 0.0
        entry_time = None
        stop_price = 0.0
        trades_today = 0
        n_contracts = 1

        for i in range(len(day_df)):
            # Check Exit on Open Position
            if position == 1:
                # Stop Loss Hit
                if lows[i] <= stop_price:
                    exit_price = round_to_tick(stop_price - slippage_pts, tick_size)
                    pnl = (exit_price - entry_price) * p['MINI_POINT_VALUE'] * n_contracts - (p['COMMISSION_MINI_RT'] * n_contracts)
                    current_equity += pnl
                    trades.append({
                        "entry_time": entry_time,
                        "exit_time": time_strs[i],
                        "direction": "long",
                        "entry_price": entry_price,
                        "exit_price": exit_price,
                        "stop_price": stop_price,
                        "net_pnl": float(round(pnl, 2)),
                        "commission": float(p['COMMISSION_MINI_RT'] * n_contracts),
                        "slippage": float(round(2 * slippage_pts * p['MINI_POINT_VALUE'] * n_contracts, 2))
                    })
                    position = 0
            elif position == -1:
                # Stop Loss Hit
                if highs[i] >= stop_price:
                    exit_price = round_to_tick(stop_price + slippage_pts, tick_size)
                    pnl = (entry_price - exit_price) * p['MINI_POINT_VALUE'] * n_contracts - (p['COMMISSION_MINI_RT'] * n_contracts)
                    current_equity += pnl
                    trades.append({
                        "entry_time": entry_time,
                        "exit_time": time_strs[i],
                        "direction": "short",
                        "entry_price": entry_price,
                        "exit_price": exit_price,
                        "stop_price": stop_price,
                        "net_pnl": float(round(pnl, 2)),
                        "commission": float(p['COMMISSION_MINI_RT'] * n_contracts),
                        "slippage": float(round(2 * slippage_pts * p['MINI_POINT_VALUE'] * n_contracts, 2))
                    })
                    position = 0

            # End of Day Flatten (15:59 / 16:00 ET)
            is_last_bar = (i == len(day_df) - 1)
            is_eod = is_last_bar or (times_h[i] == 15 and times_m[i] >= 59) or (times_h[i] >= 16)
            if is_eod:
                if position != 0:
                    exit_price = round_to_tick(closes[i] - slippage_pts if position == 1 else closes[i] + slippage_pts, tick_size)
                    pnl = (exit_price - entry_price if position == 1 else entry_price - exit_price) * p['MINI_POINT_VALUE'] * n_contracts - (p['COMMISSION_MINI_RT'] * n_contracts)
                    current_equity += pnl
                    trades.append({
                        "entry_time": entry_time,
                        "exit_time": time_strs[i],
                        "direction": "long" if position == 1 else "short",
                        "entry_price": entry_price,
                        "exit_price": exit_price,
                        "stop_price": stop_price,
                        "net_pnl": float(round(pnl, 2)),
                        "commission": float(p['COMMISSION_MINI_RT'] * n_contracts),
                        "slippage": float(round(2 * slippage_pts * p['MINI_POINT_VALUE'] * n_contracts, 2))
                    })
                    position = 0
                break # Unconditional break: NEVER allow new entries on final bar / EOD


            # Entry Logic
            if position == 0 and trades_today < p['MAX_TRADES_PER_DAY']:
                if np.isnan(fast_mas[i]) or np.isnan(slow_mas[i]) or np.isnan(atrs[i]):
                    continue
                
                # Crossover Condition
                if fast_mas[i] > slow_mas[i] and (i > 0 and fast_mas[i-1] <= slow_mas[i-1]):
                    entry_price = round_to_tick(closes[i] + slippage_pts, tick_size)
                    entry_time = time_strs[i]
                    stop_price = round_to_tick(entry_price - (p['STOP_ATR_MULT'] * atrs[i]), tick_size)
                    
                    # Immediate Same-Bar Stop Check
                    if lows[i] <= stop_price:
                        # Stopped out immediately on entry bar
                        exit_price = round_to_tick(stop_price - slippage_pts, tick_size)
                        pnl = (exit_price - entry_price) * p['MINI_POINT_VALUE'] * n_contracts - (p['COMMISSION_MINI_RT'] * n_contracts)
                        current_equity += pnl
                        trades.append({
                            "entry_time": entry_time,
                            "exit_time": time_strs[i],
                            "direction": "long",
                            "entry_price": entry_price,
                            "exit_price": exit_price,
                            "stop_price": stop_price,
                            "net_pnl": float(round(pnl, 2)),
                            "commission": float(p['COMMISSION_MINI_RT'] * n_contracts),
                            "slippage": float(round(2 * slippage_pts * p['MINI_POINT_VALUE'] * n_contracts, 2))
                        })
                    else:
                        position = 1
                    trades_today += 1

                elif fast_mas[i] < slow_mas[i] and (i > 0 and fast_mas[i-1] >= slow_mas[i-1]):
                    entry_price = round_to_tick(closes[i] - slippage_pts, tick_size)
                    entry_time = time_strs[i]
                    stop_price = round_to_tick(entry_price + (p['STOP_ATR_MULT'] * atrs[i]), tick_size)
                    
                    # Immediate Same-Bar Stop Check
                    if highs[i] >= stop_price:
                        # Stopped out immediately on entry bar
                        exit_price = round_to_tick(stop_price + slippage_pts, tick_size)
                        pnl = (entry_price - exit_price) * p['MINI_POINT_VALUE'] * n_contracts - (p['COMMISSION_MINI_RT'] * n_contracts)
                        current_equity += pnl
                        trades.append({
                            "entry_time": entry_time,
                            "exit_time": time_strs[i],
                            "direction": "short",
                            "entry_price": entry_price,
                            "exit_price": exit_price,
                            "stop_price": stop_price,
                            "net_pnl": float(round(pnl, 2)),
                            "commission": float(p['COMMISSION_MINI_RT'] * n_contracts),
                            "slippage": float(round(2 * slippage_pts * p['MINI_POINT_VALUE'] * n_contracts, 2))
                        })
                    else:
                        position = -1
                    trades_today += 1

    # 4. Compute Quant Metrics
    if trades:
        trades_df = pd.DataFrame(trades)
        trades_df['Dollar_PnL'] = trades_df['net_pnl']
        trades_df['EntryTime'] = pd.to_datetime(trades_df['entry_time'], utc=True)
        trades_df['ExitTime'] = pd.to_datetime(trades_df['exit_time'], utc=True)
        trades_df['Commission'] = trades_df.get('commission', 0.0)
        
        rf_annual = float(p.get('RISK_FREE_RATE_PCT', 4.0)) / 100.0
        stats = quant_metrics.calculate_stats(trades_df, starting_capital, rf_annual)
    else:
        stats = {}

    return {
        "trades": trades,
        "metrics": stats,
        "params": p
    }

def run_data_permutation(data, params: dict = None, n_permutations: int = 1000, seed: int = 42) -> dict:
    """
    Permutes daily bar order to evaluate strategy significance under price series disruption.
    """
    orig_res = run_backtest(data, params)
    orig_pnl = orig_res.get("metrics", {}).get("Net Profit", 0.0)
    
    df = prepare_data(data)
    unique_dates = df['date'].unique()
    
    np.random.seed(seed)
    perm_pnls = []
    
    for _ in range(min(n_permutations, 100)):
        shuffled_dates = np.random.permutation(unique_dates)
        # Create day map
        day_dfs = [df[df['date'] == d] for d in shuffled_dates]
        shuffled_df = pd.concat(day_dfs, ignore_index=True)
        
        res = run_backtest(shuffled_df, params)
        perm_pnls.append(res.get("metrics", {}).get("Net Profit", 0.0))
        
    perm_pnls = np.array(perm_pnls)
    pct_better = (np.sum(perm_pnls >= orig_pnl) / len(perm_pnls)) * 100.0
    pct_profitable = (np.sum(perm_pnls > 0) / len(perm_pnls)) * 100.0
    p_val = float(np.sum(perm_pnls >= orig_pnl) / len(perm_pnls))
    
    return {
        "original_pnl": float(orig_pnl),
        "median_pnl": float(np.median(perm_pnls)),
        "pct_profitable": float(pct_profitable),
        "pct_beating_original": float(pct_better),
        "p_value": p_val
    }
