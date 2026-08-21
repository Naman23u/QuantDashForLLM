import os
import sys
import copy
import json
import importlib.util
import pandas as pd
import numpy as np
from datetime import datetime, timezone, time as dt_time

# Add parent directory to path so we can import quant_metrics
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import quant_metrics

_MODULE_CACHE = {}

def load_script(script_path):
    if script_path in _MODULE_CACHE:
        return _MODULE_CACHE[script_path]
    if not os.path.exists(script_path):
        raise FileNotFoundError(f"Script not found at: {script_path}")
    module_name = os.path.splitext(os.path.basename(script_path))[0]
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _MODULE_CACHE[script_path] = module
    return module

def to_utc_from_et(series):
    if not isinstance(series, pd.Series):
        series = pd.Series(series)
    def parse_dt(val):
        if pd.isna(val):
            return pd.NaT
        ts = pd.to_datetime(val)
        if ts.tzinfo is None:
            ts = ts.tz_localize('America/New_York', ambiguous='NaT', nonexistent='shift_forward')
        return ts.tz_convert('UTC')
    return pd.Series([parse_dt(v) for v in series], index=series.index)


def get_strategy_warmup_bars(module, params=None):
    """
    Queries strategy for dynamic warmup requirements.
    Fail-Loud Contract:
      - If strategy defines get_warmup_requirements(), any exception raised is treated as a
        critical contract failure and re-raised (never silently masked).
      - Default fallback (1500 bars) ONLY applies if strategy does not implement get_warmup_requirements.
    """
    if hasattr(module, 'get_warmup_requirements') and callable(getattr(module, 'get_warmup_requirements')):
        try:
            req = module.get_warmup_requirements(params)
        except Exception as e:
            raise RuntimeError(f"Strategy Warmup Contract Failure: get_warmup_requirements() raised {type(e).__name__}: {e}")
            
        if isinstance(req, dict):
            intraday_bars = int(req.get('intraday_bars', req.get('bars', 0)))
            daily_sessions = int(req.get('daily_sessions', req.get('days', req.get('calendar_days', 0))))
            needs_prev_close = bool(req.get('previous_close', req.get('needs_previous_session', False)))
            
            session_bars = (daily_sessions + (1 if needs_prev_close else 0)) * 390
            calculated_bars = max(intraday_bars, session_bars)
            if calculated_bars > 0:
                return max(100, calculated_bars)
        elif isinstance(req, (int, float)):
            return int(req)
            
    if hasattr(module, 'WARMUP_BARS'):
        try:
            return int(getattr(module, 'WARMUP_BARS'))
        except Exception as e:
            raise RuntimeError(f"Strategy Warmup Contract Failure: invalid WARMUP_BARS constant: {e}")
            
    return 1500


def validate_strategy_output_schema(res):
    """
    Validates top-level return value from strategy.run_backtest().
    Must be a dictionary containing a 'trades' list.
    """
    if not isinstance(res, dict):
        raise TypeError(f"Strategy Output Schema Violation: Expected dict, got {type(res).__name__}")
    if "trades" not in res:
        raise KeyError("Strategy Output Schema Violation: Missing required 'trades' key in return dictionary.")
    if not isinstance(res["trades"], list):
        raise TypeError(f"Strategy Output Schema Violation: 'trades' must be a list, got {type(res['trades']).__name__}")
    return True


def get_strategy_contract_metadata(module):
    """
    Extracts and strictly validates strategy contract metadata:
      - Type validation (strictly bool for flags, int for limits, valid IANA tz, valid HH:MM)
      - Contradiction prevention (rejects SINGLE with max_pos > 1, pyramiding=True, hedging=True)
      - Zero silent fallbacks (raises ValueError/TypeError on invalid declarations)
    """
    if isinstance(module, dict):
        raw_pos = module.get("position_model", "SINGLE")
        raw_max_pos = module.get("max_open_positions", 1)
        raw_pyramid = module.get("allow_pyramiding", False)
        raw_hedging = module.get("allow_hedging", False)
        raw_sess_model = module.get("session_model", "INTRADAY_FLAT")
        raw_tz = module.get("session_timezone", "America/New_York")
        raw_close = module.get("session_close", "16:00")
    else:
        raw_pos = getattr(module, "POSITION_MODEL", "SINGLE")
        raw_max_pos = getattr(module, "MAX_OPEN_POSITIONS", 1)
        raw_pyramid = getattr(module, "ALLOW_PYRAMIDING", False)
        raw_hedging = getattr(module, "ALLOW_HEDGING", False)
        raw_sess_model = getattr(module, "SESSION_MODEL", "INTRADAY_FLAT")
        raw_tz = getattr(module, "SESSION_TIMEZONE", "America/New_York")
        raw_close = getattr(module, "SESSION_CLOSE", "16:00")

    # 1. POSITION_MODEL
    if not isinstance(raw_pos, str) or raw_pos.upper() not in {"SINGLE", "MULTI"}:
        raise ValueError(f"Invalid Strategy Contract: POSITION_MODEL must be 'SINGLE' or 'MULTI', got '{raw_pos}'.")
    pos_model = raw_pos.upper()

    # 2. MAX_OPEN_POSITIONS
    if not isinstance(raw_max_pos, int) or isinstance(raw_max_pos, bool) or raw_max_pos < 1:
        raise ValueError(f"Invalid Strategy Contract: MAX_OPEN_POSITIONS must be a positive integer >= 1, got '{raw_max_pos}'.")

    # 3. Boolean flags (strict type checking)
    if not isinstance(raw_pyramid, bool):
        raise TypeError(f"Invalid Strategy Contract: ALLOW_PYRAMIDING must be a boolean (True/False), got {type(raw_pyramid).__name__} ('{raw_pyramid}').")

    if not isinstance(raw_hedging, bool):
        raise TypeError(f"Invalid Strategy Contract: ALLOW_HEDGING must be a boolean (True/False), got {type(raw_hedging).__name__} ('{raw_hedging}').")

    # 4. Consistency checks for SINGLE position model
    if pos_model == "SINGLE":
        if raw_max_pos != 1:
            raise ValueError(f"Contract Contradiction: POSITION_MODEL is 'SINGLE' but MAX_OPEN_POSITIONS is {raw_max_pos} (must be 1).")
        if raw_pyramid:
            raise ValueError("Contract Contradiction: POSITION_MODEL is 'SINGLE' but ALLOW_PYRAMIDING is True.")
        if raw_hedging:
            raise ValueError("Contract Contradiction: POSITION_MODEL is 'SINGLE' but ALLOW_HEDGING is True.")

    # 5. SESSION_MODEL
    if not isinstance(raw_sess_model, str) or raw_sess_model.upper() not in {"INTRADAY_FLAT", "MULTI_DAY"}:
        raise ValueError(f"Invalid Strategy Contract: SESSION_MODEL must be 'INTRADAY_FLAT' or 'MULTI_DAY', got '{raw_sess_model}'.")
    sess_model = raw_sess_model.upper()

    # 6. SESSION_TIMEZONE
    if not isinstance(raw_tz, str):
        raise TypeError(f"Invalid Strategy Contract: SESSION_TIMEZONE must be a string, got {type(raw_tz).__name__}.")
    try:
        import zoneinfo
        _ = zoneinfo.ZoneInfo(raw_tz)
    except Exception:
        raise ValueError(f"Invalid Strategy Contract: SESSION_TIMEZONE '{raw_tz}' is not a valid IANA timezone.")

    # 7. SESSION_CLOSE
    if not isinstance(raw_close, str):
        raise TypeError(f"Invalid Strategy Contract: SESSION_CLOSE must be a string 'HH:MM', got {type(raw_close).__name__}.")
    try:
        parts = raw_close.strip().split(":")
        if len(parts) != 2:
            raise ValueError()
        ch, cm = int(parts[0]), int(parts[1])
        if not (0 <= ch <= 23 and 0 <= cm <= 59):
            raise ValueError()
    except Exception:
        raise ValueError(f"Invalid Strategy Contract: SESSION_CLOSE must be a valid 'HH:MM' time string, got '{raw_close}'.")

    return {
        "position_model": pos_model,
        "max_open_positions": raw_max_pos,
        "allow_pyramiding": raw_pyramid,
        "allow_hedging": raw_hedging,
        "session_model": sess_model,
        "session_timezone": raw_tz,
        "session_close": raw_close.strip()
    }


def validate_trade_contract(trades, contract=None, dataset_end_time=None):
    """
    Rigorously validates trade objects produced by strategy:
      - Field existence: entry_time, exit_time, direction, entry_price, exit_price, net_pnl
      - Chronological validity: entry_time < exit_time
      - Finite numbers: no NaN or Inf in numeric fields
      - Direction validity: long / short
      - Position model compliance: if SINGLE, no overlapping intraday positions
      - Session model compliance: if INTRADAY_FLAT, no overnight straddling or exits past SESSION_CLOSE
      - Dataset bounds: all trades exit on or before dataset_end_time
    """
    if not isinstance(trades, list):
        raise TypeError(f"Trade Contract Violation: trades must be a list, got {type(trades).__name__}")
        
    contract_meta = get_strategy_contract_metadata(contract if contract is not None else {})
    if not trades:
        return True
        
    required_keys = {"entry_time", "exit_time", "direction", "entry_price", "exit_price", "net_pnl"}
    parsed_trades = []
    
    for idx, t in enumerate(trades):
        if not isinstance(t, dict):
            raise TypeError(f"Trade #{idx+1} Violation: trade must be a dict, got {type(t).__name__}")
            
        missing = required_keys - set(t.keys())
        if missing:
            raise KeyError(f"Trade #{idx+1} Violation: Missing required field(s): {missing}")
            
        # Parse timestamps
        try:
            entry_dt = pd.to_datetime(t["entry_time"])
            exit_dt = pd.to_datetime(t["exit_time"])
        except Exception as e:
            raise ValueError(f"Trade #{idx+1} Timestamp Violation: Failed to parse timestamps: {e}")
            
        if pd.isna(entry_dt) or pd.isna(exit_dt):
            raise ValueError(f"Trade #{idx+1} Timestamp Violation: NaT timestamp detected.")
            
        if exit_dt < entry_dt:
            raise ValueError(f"Trade #{idx+1} Invariant Violation: exit_time ({exit_dt}) must be on or after entry_time ({entry_dt}).")
            
        # Validate finite numeric values
        for num_field in ["entry_price", "exit_price", "net_pnl"]:
            val = t[num_field]
            if not isinstance(val, (int, float, np.number)) or np.isnan(val) or np.isinf(val):
                raise ValueError(f"Trade #{idx+1} Numeric Violation: {num_field} is non-finite or NaN ({val}).")
                
        for opt_field in ["commission", "slippage", "stop_price"]:
            if opt_field in t and t[opt_field] is not None:
                val = t[opt_field]
                if not isinstance(val, (int, float, np.number)) or np.isnan(val) or np.isinf(val):
                    raise ValueError(f"Trade #{idx+1} Numeric Violation: {opt_field} is non-finite or NaN ({val}).")
                    
        # Direction validity
        dir_str = str(t["direction"]).strip().lower()
        if dir_str not in {"long", "short", "buy", "sell", "1", "-1"}:
            raise ValueError(f"Trade #{idx+1} Direction Violation: Invalid direction '{t['direction']}'.")
            
        # Session Model Check (Strict for INTRADAY_FLAT)
        if contract_meta["session_model"] == "INTRADAY_FLAT":
            session_tz = contract_meta["session_timezone"]
            session_close_str = contract_meta["session_close"]
            ch, cm = map(int, session_close_str.split(":"))
            close_time = dt_time(ch, cm)
                
            entry_loc = entry_dt.tz_localize(session_tz) if entry_dt.tzinfo is None else entry_dt.tz_convert(session_tz)
            exit_loc = exit_dt.tz_localize(session_tz) if exit_dt.tzinfo is None else exit_dt.tz_convert(session_tz)
                
            if entry_loc.date() != exit_loc.date():
                raise ValueError(f"Trade #{idx+1} Session Model Violation: INTRADAY_FLAT strategy held trade across calendar days ({entry_loc} to {exit_loc}).")
                
            if exit_loc.time() > close_time:
                raise ValueError(f"Trade #{idx+1} Session Close Violation: exit_time ({exit_loc.time()}) exceeds declared SESSION_CLOSE ({close_time}) in {session_tz}.")
                
        # Dataset End Bound Check
        if dataset_end_time is not None:
            ds_end_dt = pd.to_datetime(dataset_end_time)
            if exit_dt > ds_end_dt:
                raise ValueError(f"Trade #{idx+1} Boundary Violation: exit_time ({exit_dt}) extends past dataset end ({ds_end_dt}).")
                
        parsed_trades.append((entry_dt, exit_dt, dir_str, idx))
        
    # Overlap / Concurrency Check (using strictly validated contract_meta)
    position_model = contract_meta["position_model"]
    max_positions = contract_meta["max_open_positions"]
    allow_pyramiding = contract_meta["allow_pyramiding"]
    allow_hedging = contract_meta["allow_hedging"]
    
    if position_model == "SINGLE":

        # Sort by entry time
        parsed_trades.sort(key=lambda x: x[0])
        for k in range(1, len(parsed_trades)):
            prev_entry, prev_exit, prev_dir, prev_idx = parsed_trades[k-1]
            curr_entry, curr_exit, curr_dir, curr_idx = parsed_trades[k]
            if curr_entry < prev_exit:
                raise ValueError(
                    f"Single-Position Overlap Violation: Trade #{curr_idx+1} entered at {curr_entry} "
                    f"before prior Trade #{prev_idx+1} exited at {prev_exit}."
                )
    elif position_model == "MULTI":
        events = []
        for entry_dt, exit_dt, dir_str, idx in parsed_trades:
            events.append((entry_dt, 1, dir_str, idx))
            events.append((exit_dt, -1, dir_str, idx))
        events.sort(key=lambda x: (x[0], x[1])) # Exits before entries at identical timestamp
        
        current_longs = 0
        current_shorts = 0
        
        for dt, delta, dir_str, idx in events:
            if dir_str in {"long", "buy", "1"}:
                current_longs += delta
            else:
                current_shorts += delta
                
            total_open = current_longs + current_shorts
            if total_open > max_positions:
                raise ValueError(
                    f"Multi-Position Concurrency Violation: {total_open} simultaneous open positions at {dt}, "
                    f"exceeding MAX_OPEN_POSITIONS={max_positions}."
                )
            if not allow_pyramiding and (current_longs > 1 or current_shorts > 1):
                raise ValueError(
                    f"Pyramiding Violation: ALLOW_PYRAMIDING is False but multiple same-direction positions opened at {dt}."
                )
            if not allow_hedging and (current_longs > 0 and current_shorts > 0):
                raise ValueError(
                    f"Hedging Violation: ALLOW_HEDGING is False but simultaneous long ({current_longs}) and short ({current_shorts}) positions opened at {dt}."
                )
                
    return True


def canonicalize_strategy_output(trades, metrics=None, final_equity=None):
    """
    Canonicalizes trades and strategy outputs into deterministic normalized JSON:
      - Timestamps normalized to ISO UTC strings
      - Floats rounded to 6 decimal places
      - Dictionary keys sorted
      - Trade lists sorted by entry_time
    """
    canon_trades = []
    if isinstance(trades, list):
        for t in trades:
            c_t = {}
            for k in sorted(t.keys()):
                v = t[k]
                if isinstance(v, (datetime, pd.Timestamp)):
                    if v.tzinfo is None:
                        v = v.tz_localize('America/New_York')
                    c_t[k] = v.tz_convert('UTC').isoformat()
                elif isinstance(v, (float, np.floating)):
                    c_t[k] = round(float(v), 6)
                elif isinstance(v, (int, np.integer)):
                    c_t[k] = int(v)
                else:
                    c_t[k] = str(v)
            canon_trades.append(c_t)
            
    canon_trades.sort(key=lambda x: x.get("entry_time", ""))
    
    canon_metrics = {}
    if isinstance(metrics, dict):
        for k in sorted(metrics.keys()):
            v = metrics[k]
            if isinstance(v, (float, np.floating)):
                canon_metrics[k] = round(float(v), 6)
            elif isinstance(v, (int, np.integer)):
                canon_metrics[k] = int(v)
            else:
                canon_metrics[k] = str(v)
                
    canon_payload = {
        "trades": canon_trades,
        "metrics": canon_metrics,
        "final_equity": round(float(final_equity), 6) if final_equity is not None else None
    }
    return json.dumps(canon_payload, sort_keys=True)


def evaluate_strategy_preflight(strategy_module, sample_df, params=None):
    """
    Fast, isolated pre-flight sandbox check (< 200 ms):
      1. Function signatures: run_backtest (required), get_parameters_definition (optional)
      2. Warmup contract check (fails loudly if broken)
      3. Dual input DataFrame immutability assertions across runs
      4. Dual canonical determinism assertion across isolated runs
      5. Trade contract validation
    """
    if not hasattr(strategy_module, "run_backtest") or not callable(getattr(strategy_module, "run_backtest")):
        raise AttributeError("Pre-Flight Violation: Strategy missing callable 'run_backtest(data, params)' function.")
        
    contract = get_strategy_contract_metadata(strategy_module)
    
    # Warmup contract verification
    _ = get_strategy_warmup_bars(strategy_module, params)
    
    test_params = params.copy() if params else {}
    if hasattr(strategy_module, "get_parameters_definition") and callable(getattr(strategy_module, "get_parameters_definition")):
        p_defs = strategy_module.get_parameters_definition()
        if isinstance(p_defs, dict):
            for k, v in p_defs.items():
                if k not in test_params and isinstance(v, dict) and "default" in v:
                    test_params[k] = v["default"]
                    
    # Input Immutability Test (Run 1)
    df_copy_1 = copy.deepcopy(sample_df)
    df_snapshot_1 = copy.deepcopy(sample_df)
    
    res_1 = strategy_module.run_backtest(df_copy_1, copy.deepcopy(test_params))
    validate_strategy_output_schema(res_1)
    
    if not df_copy_1.equals(df_snapshot_1):
        raise RuntimeError("Pre-Flight Immutability Violation: Strategy mutated input DataFrame in-place on run 1.")
        
    trades_1 = res_1.get("trades", [])
    validate_trade_contract(trades_1, contract)
    
    # Determinism & Immutability Test (Run 2 with isolated deepcopy)
    df_copy_2 = copy.deepcopy(sample_df)
    df_snapshot_2 = copy.deepcopy(sample_df)
    res_2 = strategy_module.run_backtest(df_copy_2, copy.deepcopy(test_params))
    validate_strategy_output_schema(res_2)
    
    if not df_copy_2.equals(df_snapshot_2):
        raise RuntimeError("Pre-Flight Immutability Violation: Strategy mutated input DataFrame in-place on run 2.")
        
    trades_2 = res_2.get("trades", [])
    validate_trade_contract(trades_2, contract)
    
    canon_1 = canonicalize_strategy_output(trades_1, res_1.get("metrics"), res_1.get("final_equity"))
    canon_2 = canonicalize_strategy_output(trades_2, res_2.get("metrics"), res_2.get("final_equity"))
    
    if canon_1 != canon_2:
        raise RuntimeError("Pre-Flight Determinism Violation: Identical inputs produced disparate strategy outputs.")
        
    return {
        "valid": True,
        "status": "VALID",
        "contract": contract,
        "trades_count": len(trades_1)
    }



def evaluate_backtest_task(script_path, prepared_df, combo, base_params, starting_capital=100000.0, rf_annual=0.04, filter_start_date=None):
    """
    Worker task executed in parallel across multi-core ProcessPool.
    Integrates fail-safe trade contract validation, granular status tagging, and boundary flatness enforcement.
    """
    try:
        module = load_script(script_path)
        contract = get_strategy_contract_metadata(module)
        var_params = base_params.copy()
        if combo:
            var_params.update(combo)
            
        res = module.run_backtest(prepared_df, var_params)
        validate_strategy_output_schema(res)
        raw_trades = res.get("trades", [])
        
        # Discard warmup-originated trades and enforce boundary flatness
        if filter_start_date is not None and raw_trades:
            trades_list = []
            for t in raw_trades:
                entry_val = t.get('entry_time')
                exit_val = t.get('exit_time')
                if entry_val:
                    entry_date = pd.to_datetime(entry_val).date()
                    exit_date = pd.to_datetime(exit_val).date() if exit_val else entry_date
                    if entry_date < filter_start_date:
                        # Verify zero boundary straddling: Warmup trade must close strictly before evaluation window
                        if exit_date >= filter_start_date:
                            return {
                                "param_combo": combo,
                                "metrics": {},
                                "p_value": None,
                                "significant": False,
                                "trades_count": 0,
                                "trades": [],
                                "status": "INVALID_TRADE_CONTRACT",
                                "error": (
                                    f"Boundary Flatness Violation: Warmup trade entered at {entry_val} "
                                    f"remained open across evaluation boundary {filter_start_date} (exited at {exit_val})."
                                )
                            }
                        continue
                    trades_list.append(t)
        else:
            trades_list = raw_trades

        # Validate trade contract on evaluated trades
        try:
            validate_trade_contract(trades_list, contract)
        except Exception as e:
            return {
                "param_combo": combo,
                "metrics": {},
                "p_value": None,
                "significant": False,
                "trades_count": 0,
                "trades": [],
                "status": "INVALID_TRADE_CONTRACT",
                "error": f"Trade Contract Violation: {e}"
            }
        
        if trades_list:
            df = pd.DataFrame(trades_list)
            df['Dollar_PnL'] = df['net_pnl']
            df['EntryTime'] = to_utc_from_et(df['entry_time'])
            df['ExitTime'] = to_utc_from_et(df['exit_time'])
            df['Commission'] = df.get('commission', 0.0)
            stats = quant_metrics.calculate_stats(df, starting_capital, rf_annual)
            p_val = stats.get("P-Value")
            significant = (p_val is not None and p_val <= 0.05)
            status = "VALID"
        else:
            stats = {}
            p_val = None
            significant = False
            status = "NO_TRADES"
            
        return {
            "param_combo": combo,
            "metrics": stats,
            "p_value": p_val,
            "significant": significant,
            "trades_count": len(trades_list),
            "trades": trades_list,
            "status": status
        }
    except Exception as e:
        err_msg = str(e)
        status = "INVALID_WARMUP_CONTRACT" if "Warmup Contract Failure" in err_msg else "EXECUTION_ERROR"
        return {
            "param_combo": combo,
            "metrics": {},
            "p_value": None,
            "significant": False,
            "trades_count": 0,
            "trades": [],
            "status": status,
            "error": err_msg
        }


