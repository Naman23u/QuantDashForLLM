import os
import sys
import glob
import importlib.util
import webview
import calendar
from flask import Flask, jsonify, request, render_template, Response, json
import pandas as pd
import numpy as np
import quant_metrics
import worker_engine
from concurrent.futures import ProcessPoolExecutor, as_completed
import itertools

# Hardware Multiprocessing Specification (Intel i5-13450HX: 6 P-Cores / 12 Threads)
MAX_WORKERS = 10

# Add parent directory to path so we can import the strategies if needed
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if getattr(sys, 'frozen', False):
    bundle_dir = getattr(sys, '_MEIPASS', os.path.dirname(sys.executable))
    app = Flask(__name__, 
                template_folder=os.path.join(bundle_dir, 'templates'), 
                static_folder=os.path.join(bundle_dir, 'static'))
else:
    app = Flask(__name__)
last_df = None


ET_ZONE = 'America/New_York'

def normalize_databento_data(df):
    """
    Standardize Databento and generic financial Parquet/CSV DataFrames.
    Ensures 'ts_event' column exists (extracting from index or timestamp/datetime/ts_recv columns),
    OHLCV column names are standard lowercase, and Databento fixed-point prices are scaled.
    """
    df = df.copy()

    # 1. Extract timestamp from index if index is DatetimeIndex or named timestamp/ts_event/datetime/time/date
    if isinstance(df.index, pd.DatetimeIndex) or (df.index.name and str(df.index.name).lower() in ['timestamp', 'ts_event', 'datetime', 'date', 'time']):
        if 'ts_event' not in df.columns:
            df['ts_event'] = df.index
        df = df.reset_index(drop=True)

    # 2. Find and standardize Timestamp Column -> 'ts_event'
    if 'ts_event' not in df.columns:
        possible_ts_cols = ['timestamp', 'datetime', 'ts_recv', 'ts_out', 'time', 'date', 'Date', 'Time', 'DateTime', 'Datetime']
        found_col = None
        for col in possible_ts_cols:
            if col in df.columns:
                found_col = col
                break
        if found_col:
            df['ts_event'] = df[found_col]
        else:
            for col in df.columns:
                if pd.api.types.is_datetime64_any_dtype(df[col]):
                    df['ts_event'] = df[col]
                    break

    if 'ts_event' not in df.columns:
        raise KeyError("Could not find a valid timestamp column or index (ts_event, timestamp, datetime, etc.) in dataset.")

    # 3. Ensure ts_event is UTC datetime
    if not pd.api.types.is_datetime64_any_dtype(df['ts_event']):
        if pd.api.types.is_numeric_dtype(df['ts_event']):
            df['ts_event'] = pd.to_datetime(df['ts_event'], unit='ns', utc=True)
        else:
            df['ts_event'] = pd.to_datetime(df['ts_event'], utc=True)
    elif df['ts_event'].dt.tz is None:
        df['ts_event'] = df['ts_event'].dt.tz_localize('UTC')

    # 4. Standardize OHLCV column names
    col_map = {}
    for col in df.columns:
        c_lower = str(col).lower()
        if c_lower in ['open', 'o'] and 'open' not in df.columns:
            col_map[col] = 'open'
        elif c_lower in ['high', 'h'] and 'high' not in df.columns:
            col_map[col] = 'high'
        elif c_lower in ['low', 'l'] and 'low' not in df.columns:
            col_map[col] = 'low'
        elif c_lower in ['close', 'c', 'price'] and 'close' not in df.columns:
            col_map[col] = 'close'
        elif c_lower in ['volume', 'vol', 'size', 'qty', 'real_volume', 'tick_volume'] and 'volume' not in df.columns:
            col_map[col] = 'volume'
            
    if col_map:
        df = df.rename(columns=col_map)

    # 5. Fix Databento Fixed-Point Price Scaling if needed (close > 1e7)
    if 'close' in df.columns:
        close_max = df['close'].abs().max()
        if close_max > 1e7:
            scale_factor = 1e9 if close_max > 1e11 else 1e7
            for col in ['open', 'high', 'low', 'close']:
                if col in df.columns:
                    df[col] = df[col] / scale_factor

    return df


def to_utc_from_et(series):
    if not isinstance(series, pd.Series):
        series = pd.Series(series)
    def convert_val(val):
        if pd.isna(val):
            return pd.NaT
        ts = pd.to_datetime(val)
        if ts.tzinfo is None:
            ts = ts.tz_localize(ET_ZONE, ambiguous='NaT', nonexistent='shift_forward')
        return ts.tz_convert('UTC')
    return pd.Series([convert_val(v) for v in series], index=series.index)

def load_script(script_path):
    # Dynamically import the python file from absolute path
    if not os.path.exists(script_path):
        raise FileNotFoundError(f"Script not found at {script_path}")
    script_name = os.path.basename(script_path).replace('.py', '')
    spec = importlib.util.spec_from_file_location(script_name, script_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def get_quantdash_dirs():
    """Returns the explicit paths for QuantDash Strategy_Files and Market_Data with smart fallback."""
    # 1. Strategy directory candidate search order
    strat_candidates = [
        os.path.join(BASE_DIR, 'Strategy_Files'),
        os.path.join(os.path.dirname(BASE_DIR), 'Strategy_Files'),
        os.path.join(os.path.dirname(os.path.dirname(BASE_DIR)), 'Strategy_Files'),
        r"C:\Users\trade\Downloads\The Akashic Records\QuantDash\Strategy_Files"
    ]
    if getattr(sys, 'frozen', False) and getattr(sys, '_MEIPASS', None):
        strat_candidates.insert(1, os.path.join(sys._MEIPASS, 'Strategy_Files'))

    strat_dir = None
    for cand in strat_candidates:
        if cand and os.path.exists(cand):
            try:
                py_files = [f for f in os.listdir(cand) if f.endswith('.py') and not f.startswith('__')]
                if py_files:
                    strat_dir = cand
                    break
            except Exception:
                pass
            if strat_dir is None:
                strat_dir = cand

    if not strat_dir:
        strat_dir = os.path.join(BASE_DIR, 'Strategy_Files')

    # 2. Market Data directory candidate search order
    data_candidates = [
        os.path.join(BASE_DIR, 'Market_Data'),
        os.path.join(os.path.dirname(BASE_DIR), 'Market_Data'),
        os.path.join(os.path.dirname(os.path.dirname(BASE_DIR)), 'Market_Data'),
        r"C:\Users\trade\Downloads\The Akashic Records\QuantDash\Market_Data"
    ]
    if getattr(sys, 'frozen', False) and getattr(sys, '_MEIPASS', None):
        data_candidates.insert(1, os.path.join(sys._MEIPASS, 'Market_Data'))

    data_dir = None
    for cand in data_candidates:
        if cand and os.path.exists(cand):
            try:
                mkt_files = [f for f in os.listdir(cand) if f.lower().endswith(('.parquet', '.csv'))]
                if mkt_files:
                    data_dir = cand
                    break
            except Exception:
                pass
            if data_dir is None:
                data_dir = cand

    if not data_dir:
        data_dir = os.path.join(BASE_DIR, 'Market_Data')

    return strat_dir, data_dir


def list_local_strategies():
    """Find all .py strategy files inside QuantDash/Strategy_Files."""
    strategies = []
    strat_dir, _ = get_quantdash_dirs()
    if os.path.exists(strat_dir):
        for root, dirs, files in os.walk(strat_dir):
            for file in files:
                if file.endswith('.py') and not file.startswith('__'):
                    full_path = os.path.abspath(os.path.join(root, file))
                    rel_path = os.path.relpath(full_path, strat_dir).replace('\\', '/')
                    display_name = rel_path if '/' in rel_path else file
                    strategies.append({
                        "name": file,
                        "display_name": display_name,
                        "relative_path": rel_path,
                        "path": full_path
                    })
    strategies.sort(key=lambda x: x["display_name"].lower())
    return strategies

def list_local_data():
    """Find all .parquet and .csv files inside QuantDash/Market_Data."""
    datasets = []
    _, data_dir = get_quantdash_dirs()
    if os.path.exists(data_dir):
        for root, dirs, files in os.walk(data_dir):
            for file in files:
                if file.lower().endswith(('.parquet', '.csv')):
                    full_path = os.path.abspath(os.path.join(root, file))
                    rel_path = os.path.relpath(full_path, data_dir).replace('\\', '/')
                    display_name = rel_path if '/' in rel_path else file
                    datasets.append({
                        "name": file,
                        "display_name": display_name,
                        "relative_path": rel_path,
                        "path": full_path
                    })
    datasets.sort(key=lambda x: x["display_name"].lower())
    return datasets

@app.route('/api/local_strategies', methods=['GET'])
def get_local_strategies():
    try:
        strategies = list_local_strategies()
        return jsonify({"strategies": strategies})
    except Exception as e:
        return jsonify({"error": str(e), "strategies": []}), 500

@app.route('/api/local_data', methods=['GET'])
def get_local_data():
    try:
        datasets = list_local_data()
        return jsonify({"datasets": datasets})
    except Exception as e:
        return jsonify({"error": str(e), "datasets": []}), 500

def quarter_to_dates(quarter_str):
    """
    Converts '2018-Q1' or '2018 (Q1)' to ('2018-01-01', '2018-03-31')
    """
    if not quarter_str:
        return None, None
    clean = str(quarter_str).replace('(', '').replace(')', '').replace(' ', '-').replace('_', '-')
    parts = [p for p in clean.split('-') if p]
    if len(parts) < 2:
        return None, None
    try:
        year = int(parts[0])
        q_str = parts[1].upper().replace('Q', '')
        q = int(q_str)
        
        start_months = {1: '01-01', 2: '04-01', 3: '07-01', 4: '10-01'}
        end_months = {1: '03-31', 2: '06-30', 3: '09-30', 4: '12-31'}
        
        start_date = f"{year}-{start_months[q]}"
        end_date = f"{year}-{end_months[q]}"
        return start_date, end_date
    except Exception:
        return None, None

def get_dataset_quarter_range(data_paths):
    """
    Inspects dataset file(s) and returns min_quarter, max_quarter, and list of quarters.
    """
    if isinstance(data_paths, str):
        data_paths = [data_paths]
    
    min_date = None
    max_date = None
    
    for dp in data_paths:
        if not os.path.exists(dp):
            continue
        try:
            if dp.lower().endswith('.parquet'):
                df_head = pd.read_parquet(dp).head(1)
                df_tail = pd.read_parquet(dp).tail(1)
                df_combined = pd.concat([df_head, df_tail])
                df_norm = normalize_databento_data(df_combined)
                cur_min = df_norm['ts_event'].min()
                cur_max = df_norm['ts_event'].max()
            else: # CSV
                df = pd.read_csv(dp, nrows=5)
                df_norm = normalize_databento_data(df)
                cur_min = df_norm['ts_event'].min()
                cur_max = df_norm['ts_event'].max()
                
            if cur_min is not None and not pd.isna(cur_min):
                if min_date is None or cur_min < min_date:
                    min_date = cur_min
            if cur_max is not None and not pd.isna(cur_max):
                if max_date is None or cur_max > max_date:
                    max_date = cur_max
        except Exception as e:
            print(f"Error inspecting date range for {dp}: {e}")
            
    if min_date is None or max_date is None:
        min_year, min_q = 2016, 1
        max_year, max_q = 2026, 3
    else:
        min_year = min_date.year
        min_q = (min_date.month - 1) // 3 + 1
        max_year = max_date.year
        max_q = (max_date.month - 1) // 3 + 1
        
    quarters = []
    y = min_year
    q = min_q
    while (y < max_year) or (y == max_year and q <= max_q):
        quarters.append(f"{y}-Q{q}")
        q += 1
        if q > 4:
            q = 1
            y += 1
            
    return {
        "min_quarter": quarters[0] if quarters else "2016-Q1",
        "max_quarter": quarters[-1] if quarters else "2026-Q3",
        "quarters": quarters
    }

def quantdash_load_and_prepare_data(data_path):
    """
    Universal QuantDash fallback data loader for Parquet/CSV files.
    Loads, normalizes timestamps to UTC, converts to US/Eastern, filters to RTH (09:30-16:00 ET).
    """
    if isinstance(data_path, str):
        data_paths = [data_path]
    else:
        data_paths = list(data_path)

    dfs = []
    for dp in data_paths:
        if not os.path.exists(dp):
            continue
        if dp.lower().endswith('.csv'):
            raw_df = pd.read_csv(dp)
        else:
            raw_df = pd.read_parquet(dp)
        dfs.append(normalize_databento_data(raw_df))

    if not dfs:
        return None

    df = pd.concat(dfs, ignore_index=True)
    df["ts_et"] = df["ts_event"].dt.tz_convert(ET_ZONE)

    # RTH filter: 09:30:00 to 16:00:00 ET
    rth_start = pd.Timestamp("09:30:00").time()
    rth_end = pd.Timestamp("16:00:00").time()
    df = df[(df["ts_et"].dt.time >= rth_start) & (df["ts_et"].dt.time < rth_end)].copy()
    df["date"] = df["ts_et"].dt.date
    df.sort_values("ts_et", inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df

def prepare_filtered_data(module, data_path, start_quarter=None, end_quarter=None):
    """
    Universal data loader and quarter slicer.
    Guarantees strict quarter filtering even if a strategy module does not implement load_and_prepare_data().
    """
    if hasattr(module, 'load_and_prepare_data'):
        df = module.load_and_prepare_data(data_path)
    else:
        df = quantdash_load_and_prepare_data(data_path)

    if df is None or df.empty:
        return df

    if start_quarter and end_quarter:
        start_dt, _ = quarter_to_dates(start_quarter)
        _, end_dt = quarter_to_dates(end_quarter)
        if start_dt and end_dt:
            start_date_obj = pd.to_datetime(start_dt).date()
            end_date_obj = pd.to_datetime(end_dt).date()
            if 'date' in df.columns:
                df = df[(df['date'] >= start_date_obj) & (df['date'] <= end_date_obj)].copy()
            elif 'ts_event' in df.columns:
                st_ts = pd.to_datetime(start_dt, utc=True)
                end_ts = pd.to_datetime(end_dt + ' 23:59:59', utc=True)
                df = df[(df['ts_event'] >= st_ts) & (df['ts_event'] <= end_ts)].copy()

    df.reset_index(drop=True, inplace=True)
    return df

@app.route('/api/data_quarters', methods=['GET', 'POST'])
def api_data_quarters():
    try:
        if request.method == 'POST':
            req = request.json or {}
            data_path = req.get('data')
        else:
            data_path = request.args.get('data')
            if data_path and (data_path.startswith('[') or ',' in data_path):
                try:
                    data_path = json.loads(data_path)
                except Exception:
                    data_path = data_path.split(',')
                    
        if not data_path:
            return jsonify({"quarters": ["2016-Q1", "2018-Q1", "2026-Q3"]})
            
        res = get_dataset_quarter_range(data_path)
        return jsonify(res)
    except Exception as e:
        return jsonify({"error": str(e), "quarters": []}), 500

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/select_script', methods=['GET'])
def select_script():
    try:
        if len(webview.windows) > 0:
            window = webview.windows[0]
            result = window.create_file_dialog(webview.OPEN_DIALOG, allow_multiple=False, file_types=('Python Files (*.py)', 'All Files (*.*)'))
            if result and len(result) > 0:
                return jsonify({"path": result[0]})
        return jsonify({"path": None})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/select_data', methods=['GET'])
def select_data():
    try:
        if len(webview.windows) > 0:
            window = webview.windows[0]
            result = window.create_file_dialog(webview.OPEN_DIALOG, allow_multiple=False, file_types=('Data Files (*.parquet;*.csv)', 'All Files (*.*)'))
            if result and len(result) > 0:
                return jsonify({"path": result[0]})
        return jsonify({"path": None})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/select_data_multiple', methods=['GET'])
def select_data_multiple():
    try:
        if len(webview.windows) > 0:
            window = webview.windows[0]
            result = window.create_file_dialog(webview.OPEN_DIALOG, allow_multiple=True, file_types=('Data Files (*.parquet;*.csv)', 'All Files (*.*)'))
            if result and len(result) > 0:
                return jsonify({"paths": list(result)})
        return jsonify({"paths": []})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/params', methods=['GET'])
def get_params():
    script_path = request.args.get('script')
    if not script_path:
        return jsonify({"error": "No script provided"}), 400
    try:
        module = load_script(script_path)
        if hasattr(module, 'get_default_parameters') and callable(module.get_default_parameters):
            params = module.get_default_parameters()
        elif hasattr(module, 'DEFAULT_PARAMS') and isinstance(module.DEFAULT_PARAMS, dict):
            params = module.DEFAULT_PARAMS
        elif hasattr(module, 'default_params') and isinstance(module.default_params, dict):
            params = module.default_params
        else:
            params = {}
        return jsonify(params)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

import sys
import os

# Store presets in user's AppData directory so they survive app rebuilds
app_data_dir = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'QuantDash')
os.makedirs(app_data_dir, exist_ok=True)
PRESETS_FILE = os.path.join(app_data_dir, 'presets.json')

@app.route('/api/presets', methods=['GET'])
def get_presets():
    if not os.path.exists(PRESETS_FILE):
        return jsonify({"presets": {}})
    try:
        with open(PRESETS_FILE, 'r') as f:
            data = json.load(f)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/presets', methods=['POST'])
def save_preset():
    req = request.json
    name = req.get('name')
    script_path = req.get('script')
    data_path = req.get('data')
    
    if not name or not script_path or not data_path:
        return jsonify({"error": "Missing name, script, or data path"}), 400
        
    try:
        presets_data = {"presets": {}}
        if os.path.exists(PRESETS_FILE):
            try:
                with open(PRESETS_FILE, 'r') as f:
                    presets_data = json.load(f)
            except json.JSONDecodeError:
                pass
                
        if "presets" not in presets_data:
            presets_data["presets"] = {}
            
        presets_data["presets"][name] = {
            "script": script_path,
            "data": data_path,
            "start_quarter": req.get("start_quarter"),
            "end_quarter": req.get("end_quarter"),
            "params": req.get("params", {})
        }
        
        with open(PRESETS_FILE, 'w') as f:
            json.dump(presets_data, f, indent=4)
            
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/presets', methods=['DELETE'])
def delete_preset():
    req = request.json
    name = req.get('name')
    if not name:
        return jsonify({"error": "Missing layout name"}), 400
        
    try:
        if not os.path.exists(PRESETS_FILE):
            return jsonify({"error": "No presets found"}), 404
            
        with open(PRESETS_FILE, 'r') as f:
            presets_data = json.load(f)
            
        if "presets" in presets_data and name in presets_data["presets"]:
            del presets_data["presets"][name]
            
            with open(PRESETS_FILE, 'w') as f:
                json.dump(presets_data, f, indent=4)
            return jsonify({"success": True})
        else:
            return jsonify({"error": "Layout not found"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500

MM_PRESETS_FILE = os.path.join(app_data_dir, 'mm_presets.json')

@app.route('/api/mm_presets', methods=['GET'])
def get_mm_presets():
    if not os.path.exists(MM_PRESETS_FILE):
        return jsonify({"presets": {}})
    try:
        with open(MM_PRESETS_FILE, 'r') as f:
            data = json.load(f)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/mm_presets', methods=['POST'])
def save_mm_preset():
    req = request.json
    name = req.get('name')
    data_paths = req.get('data_paths')
    
    if not name or not data_paths:
        return jsonify({"error": "Missing name or data paths"}), 400
        
    try:
        presets_data = {"presets": {}}
        if os.path.exists(MM_PRESETS_FILE):
            try:
                with open(MM_PRESETS_FILE, 'r') as f:
                    presets_data = json.load(f)
            except json.JSONDecodeError:
                pass
                
        if "presets" not in presets_data:
            presets_data["presets"] = {}
            
        presets_data["presets"][name] = {
            "data_paths": data_paths
        }
        
        with open(MM_PRESETS_FILE, 'w') as f:
            json.dump(presets_data, f, indent=4)
            
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/mm_presets', methods=['DELETE'])
def delete_mm_preset():
    req = request.json
    name = req.get('name')
    if not name:
        return jsonify({"error": "Missing layout name"}), 400
        
    try:
        if not os.path.exists(MM_PRESETS_FILE):
            return jsonify({"error": "No presets found"}), 404
            
        with open(MM_PRESETS_FILE, 'r') as f:
            presets_data = json.load(f)
            
        if "presets" in presets_data and name in presets_data["presets"]:
            del presets_data["presets"][name]
            
            with open(MM_PRESETS_FILE, 'w') as f:
                json.dump(presets_data, f, indent=4)
            return jsonify({"success": True})
        else:
            return jsonify({"error": "Layout not found"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/run', methods=['POST'])
def run_backtest():
    req = request.json
    script_path = req.get('script')
    data_path = req.get('data')
    params = req.get('params', {})
    
    if not script_path or not data_path:
        return jsonify({"error": "Missing script or data path"}), 400
        
    try:
        module = load_script(script_path)
        start_q = req.get('start_quarter')
        end_q = req.get('end_quarter')
        
        prepared_data = prepare_filtered_data(module, data_path, start_q, end_q)
        raw_res = module.run_backtest(prepared_data, params)
        if "error" in raw_res:
            with open('error.log', 'w') as f: f.write(str(raw_res["error"]))
            return jsonify(raw_res)
            
        trades_list = raw_res.get("trades", [])
        if not trades_list:
            return jsonify({"error": "No trades generated in selected period"})
            
        df = pd.DataFrame(trades_list)
        df['Dollar_PnL'] = df['net_pnl']
        df['EntryTime'] = to_utc_from_et(df['entry_time'])
        df['ExitTime'] = to_utc_from_et(df['exit_time'])
        df['Type'] = df['direction'].apply(lambda x: 'Long' if x == 'long' else 'Short')
        df['Commission'] = df.get('commission', 0.0)
        
        starting_capital = 100000.0
        rf_annual = float(params.get('RISK_FREE_RATE_PCT', 4.0)) / 100.0
        df = df.sort_values('ExitTime').reset_index(drop=True)
        
        global last_df
        last_df = df.copy()
        
        all_stats = quant_metrics.calculate_stats(df, starting_capital, rf_annual)
        df['Cumulative_PnL'] = df['Dollar_PnL'].cumsum()
        
        long_df = df[df['Type'] == 'Long'].copy()
        long_df['Cumulative_PnL'] = long_df['Dollar_PnL'].cumsum()
        
        short_df = df[df['Type'] == 'Short'].copy()
        short_df['Cumulative_PnL'] = short_df['Dollar_PnL'].cumsum()
        
        long_stats = quant_metrics.calculate_stats(long_df, starting_capital, rf_annual)
        short_stats = quant_metrics.calculate_stats(short_df, starting_capital, rf_annual)
        
        monthly_heatmap = quant_metrics.get_monthly_heatmap(df, starting_capital)
        long_monthly_heatmap = quant_metrics.get_monthly_heatmap(long_df, starting_capital)
        short_monthly_heatmap = quant_metrics.get_monthly_heatmap(short_df, starting_capital)
        
        best_worst_moments = quant_metrics.get_best_worst_moments(df, starting_capital)
        dd_analysis_table, dd_analysis_curve = quant_metrics.get_drawdown_analysis(df, starting_capital)
        trading_time_dist = quant_metrics.get_trading_time_distribution(df, starting_capital)
        dow_dist = quant_metrics.get_day_of_week_distribution(df, starting_capital)
        
        def get_curve(sub_df):
            if sub_df.empty: return {}
            pct = (sub_df['Cumulative_PnL'] / starting_capital) * 100
            peak = pct.cummax()
            dd = pct - peak
            peak = np.maximum(0, peak)
            dd = pct - peak
            return {
                "pct": pct.round(2).tolist(),
                "dd": dd.round(2).tolist(),
                "dates": sub_df['ExitTime'].dt.strftime('%Y-%m-%d %H:%M').tolist()
            }
            
        return jsonify({
            "metrics": all_stats,
            "long_metrics": long_stats,
            "short_metrics": short_stats,
            "monthly_heatmap": monthly_heatmap,
            "long_monthly_heatmap": long_monthly_heatmap,
            "short_monthly_heatmap": short_monthly_heatmap,
            "best_worst_moments": best_worst_moments,
            "dd_analysis_table": dd_analysis_table,
            "dd_analysis_curve": dd_analysis_curve,
            "trading_time_dist": trading_time_dist,
            "dow_dist": dow_dist,
            "equity_curve": get_curve(df),
            "long_equity_curve": get_curve(long_df),
            "short_equity_curve": get_curve(short_df)
        })
    except Exception as e:
        import traceback
        with open('error.log', 'w') as f: f.write(traceback.format_exc())
        return jsonify({"error": str(e)}), 500

@app.route('/api/run_monte_carlo', methods=['POST'])
def run_mc():
    global last_df
    if last_df is None or last_df.empty:
        return jsonify({"error": "No trades available. Please run backtest first."}), 400
        
    req = request.json
    starting_capital = 100000.0
    num_sims = int(req.get('num_simulations', 10000))
    pct_trades = float(req.get('pct_trades', 100.0))
    ruin_threshold = float(req.get('ruin_threshold', 50.0))
    method = req.get('method', 'Bootstrap (Resample)')
    
    try:
        mc_results = quant_metrics.run_monte_carlo(
            last_df, 
            starting_capital, 
            num_simulations=num_sims, 
            pct_trades=pct_trades, 
            ruin_threshold=ruin_threshold, 
            method=method
        )
        return jsonify(mc_results)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/run_bootstrap', methods=['POST'])
def run_bootstrap():
    global last_df
    if last_df is None or last_df.empty:
        return jsonify({"error": "No trades available. Please run backtest first."}), 400
        
    req = request.json or {}
    starting_capital = float(req.get('starting_capital', 100000.0))
    num_sims = int(req.get('num_simulations', 5000))
    method = req.get('method', 'both') # 'stationary', 'iid', or 'both'
    block_length = req.get('block_length', 'auto')
    seed = req.get('seed', 42)
    dd_thresholds = req.get('dd_thresholds', [10.0, 15.0, 20.0, 25.0, 30.0])
    rf_annual = float(req.get('rf_annual', 0.04))
    
    try:
        bootstrap_results = quant_metrics.run_bootstrap_diagnostics(
            last_df,
            starting_capital=starting_capital,
            num_simulations=num_sims,
            method=method,
            block_length=block_length,
            seed=seed,
            dd_thresholds=dd_thresholds,
            rf_annual=rf_annual
        )
        if bootstrap_results is None:
            return jsonify({"error": "Could not compute bootstrap diagnostics from current trades."}), 400
        return jsonify(bootstrap_results)
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "traceback": traceback.format_exc()}), 500


def create_synthetic_smoke_data(days=5):
    """
    Creates isolated 5-day synthetic 1-minute OHLCV DataFrame in America/New_York
    for fast pre-flight sandbox smoke testing.
    """
    dates = pd.date_range("2023-01-09 09:30:00", periods=days*390, freq="1min", tz="America/New_York")
    rth_dates = [d for d in dates if (d.hour == 9 and d.minute >= 30) or (10 <= d.hour < 16) or (d.hour == 16 and d.minute == 0)]
    n = len(rth_dates)
    np.random.seed(42)
    p = 4000.0 + np.cumsum(np.random.normal(0.05, 0.75, size=n))
    df = pd.DataFrame({
        'ts_event': rth_dates,
        'ts_et': rth_dates,
        'open': p,
        'high': p + np.abs(np.random.normal(0.5, 0.25, size=n)),
        'low': p - np.abs(np.random.normal(0.5, 0.25, size=n)),
        'close': p + np.random.normal(0.0, 0.3, size=n),
        'volume': np.random.randint(100, 5000, size=n),
        'date': [d.date() for d in rth_dates]
    })
    return df


@app.route('/api/validate_strategy', methods=['POST'])
def validate_strategy_endpoint():
    """
    Fast pre-flight sandbox strategy validator (< 200 ms).
    Tests syntax, signatures, fail-loud warmup, input immutability,
    dual canonical determinism, and trade contracts.
    """
    req = request.json or {}
    script_path = req.get('script')
    data_path = req.get('data')
    params = req.get('params', {})
    
    if not script_path:
        return jsonify({"error": "Missing script path"}), 400
        
    try:
        module = load_script(script_path)
        if data_path and os.path.exists(data_path):
            sample_df = prepare_filtered_data(module, data_path)
            if sample_df is not None and len(sample_df) > 500:
                sample_df = sample_df.iloc[:1950].copy() # 5 trading days
            else:
                sample_df = create_synthetic_smoke_data()
        else:
            sample_df = create_synthetic_smoke_data()
            
        res = worker_engine.evaluate_strategy_preflight(module, sample_df, params)
        return jsonify(res)
    except Exception as e:
        import traceback
        return jsonify({
            "valid": False,
            "status": "INVALID",
            "error": str(e),
            "traceback": traceback.format_exc()
        }), 400


@app.route('/api/robustness', methods=['POST'])

def run_robustness():
    req = request.json
    script_path = req.get('script')
    data_path = req.get('data')
    base_params = req.get('params', {})
    try:
        shift_pct = float(req.get('shift_pct', 25.0))
    except (TypeError, ValueError):
        shift_pct = 25.0
    shift_pct = max(1.0, min(shift_pct, 200.0))  # sane bounds
    shift_frac = shift_pct / 100.0

    if not script_path or not data_path:
        return jsonify({"error": "Missing script or data path"}), 400
        
    try:
        module = load_script(script_path)
        start_q = req.get('start_quarter')
        end_q = req.get('end_quarter')
        prepared_data = prepare_filtered_data(module, data_path, start_q, end_q)
        if prepared_data is None or (isinstance(prepared_data, pd.DataFrame) and prepared_data.empty):
            return jsonify({"error": "No data files loaded in selected period"}), 400

        # Parameter Shift Testing: create variations for numeric params.
        # Execution constants (tick size, commissions, point value, risk-free rate, risk capital)
        # are excluded from tuning sweeps, while strategy parameters AND slippage are tested.
        EXCLUDED_ROBUSTNESS_PARAMS = {
            "TICK_SIZE", "COMMISSION_MINI_RT", "COMMISSION_MICRO_RT",
            "MINI_POINT_VALUE", "MICRO_POINT_VALUE", "RISK_FREE_RATE_PCT",
            "MAX_TRADES_PER_DAY", "RISK_TYPE ($ or %)", "RISK_VALUE"
        }
        variations = []
        
        # Original
        variations.append({"name": "Base", "params": base_params.copy()})
        
        # +shift% variation
        up_params = base_params.copy()
        for k, v in up_params.items():
            if isinstance(v, (int, float)):
                k_upper = k.upper()
                is_excluded = (k in EXCLUDED_ROBUSTNESS_PARAMS) or (k_upper == "TICK_SIZE") or k_upper.startswith("COMMISSION") or k_upper.startswith("MINI_POINT") or k_upper.startswith("MICRO_POINT") or ("RISK_TYPE" in k_upper)
                if not is_excluded:
                    new_val = v * (1.0 + shift_frac)
                    up_params[k] = int(round(new_val)) if isinstance(v, int) else round(new_val, 4)
        variations.append({"name": f"+{shift_pct:g}% Shift", "params": up_params})
        
        # -shift% variation
        down_params = base_params.copy()
        for k, v in down_params.items():
            if isinstance(v, (int, float)):
                k_upper = k.upper()
                is_excluded = (k in EXCLUDED_ROBUSTNESS_PARAMS) or (k_upper == "TICK_SIZE") or k_upper.startswith("COMMISSION") or k_upper.startswith("MINI_POINT") or k_upper.startswith("MICRO_POINT") or ("RISK_TYPE" in k_upper)
                if not is_excluded:
                    new_val = v * (1.0 - shift_frac)
                    down_params[k] = int(round(new_val)) if isinstance(v, int) else round(new_val, 4)
        variations.append({"name": f"-{shift_pct:g}% Shift", "params": down_params})
        
        # Multi-Core Parallel Evaluation across 10 workers
        results_list = []
        rf_annual = float(base_params.get('RISK_FREE_RATE_PCT', 4.0)) / 100.0
        
        with ProcessPoolExecutor(max_workers=min(len(variations), MAX_WORKERS)) as executor:
            future_to_var = {
                executor.submit(worker_engine.evaluate_backtest_task, script_path, prepared_data, var["params"], {}, 100000.0, rf_annual): var
                for var in variations
            }
            for future in future_to_var:
                task_res = future.result()
                var = future_to_var[future]
                res = {
                    "variation": var["name"],
                    "params": var["params"],
                    "metrics": task_res.get("metrics", {}),
                    "p_value": task_res.get("p_value"),
                    "significant": task_res.get("significant", False)
                }
                results_list.append(res)
            
        return jsonify(results_list)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/optimize', methods=['POST'])
def run_optimization():
    req = request.json
    script_path = req.get('script')
    data_path = req.get('data')
    base_params = req.get('base_params', {})
    optimizable_params = req.get('optimizable_params', [])
    
    if not script_path or not data_path:
        return jsonify({"error": "Missing script or data path"}), 400
    if not optimizable_params:
        return jsonify({"error": "No optimizable parameters provided"}), 400
        
    param_ranges = {}
    total_combinations = 1
    
    for p in optimizable_params:
        name = p.get('name')
        start = float(p.get('start', 0))
        end = float(p.get('end', 0))
        step = float(p.get('step', 0))
        
        if step <= 0:
            return jsonify({"error": f"Invalid step size {step} for {name}"}), 400
        if start > end:
            return jsonify({"error": f"Start value must be <= End value for {name}"}), 400
            
        # Generate range array
        is_int = step.is_integer() and start.is_integer()
        current_val = start
        vals = []
        while current_val <= end + (step * 0.0001):
            val_to_use = int(round(current_val)) if is_int else round(current_val, 4)
            vals.append(val_to_use)
            current_val += step
            
        param_ranges[name] = vals
        total_combinations *= len(vals)
        
    if total_combinations > 5000:
        return jsonify({"error": f"Too many combinations ({total_combinations}). Maximum allowed is 5000."}), 400
        
    def generate():
        try:
            module = load_script(script_path)
            start_q = req.get('start_quarter')
            end_q = req.get('end_quarter')
            prepared_data = prepare_filtered_data(module, data_path, start_q, end_q)
            if prepared_data is None or (isinstance(prepared_data, pd.DataFrame) and prepared_data.empty):
                yield json.dumps({"error": "No data files loaded in selected period"}) + "\n"
                return

            keys, values = zip(*param_ranges.items())
            permutations = [dict(zip(keys, v)) for v in itertools.product(*values)]
            rf_annual = float(base_params.get('RISK_FREE_RATE_PCT', 4.0)) / 100.0
            
            # Multi-Core Parallel Evaluation across 10 workers
            all_task_results = []
            with ProcessPoolExecutor(max_workers=MAX_WORKERS) as executor:
                future_to_combo = {
                    executor.submit(worker_engine.evaluate_backtest_task, script_path, prepared_data, combo, base_params, 100000.0, rf_annual): combo
                    for combo in permutations
                }
                
                for future in as_completed(future_to_combo):
                    task_res = future.result()
                    combo = future_to_combo[future]
                    all_task_results.append((combo, task_res))

            # Extract trial Sharpe vector across valid trials with trades (do not contaminate with 0.0 for no-trade runs)
            trial_sharpes = []
            for combo, task_res in all_task_results:
                stat = task_res.get("metrics", {})
                t_count = task_res.get("trades_count", 0)
                if t_count > 0 and "Sharpe" in stat and stat["Sharpe"] is not None:
                    trial_sharpes.append(float(stat["Sharpe"]))

            exp_max_sr, sr_std, k_count = quant_metrics.calculate_expected_max_sharpe(trials_sr_list=trial_sharpes)


            # Stream each combination with full DSR selection-bias adjustment
            for i, (combo, task_res) in enumerate(all_task_results):
                trades = task_res.get("trades", [])
                stat = task_res.get("metrics", {})
                if trades:
                    tdf = pd.DataFrame(trades)
                    tdf['Dollar_PnL'] = tdf['net_pnl']
                    tdf['EntryTime'] = to_utc_from_et(tdf['entry_time'])
                    tdf['ExitTime'] = to_utc_from_et(tdf['exit_time'])
                    tdf['Commission'] = tdf.get('commission', 0.0)
                    stat = quant_metrics.calculate_stats(tdf, 100000.0, rf_annual, trials_sr_list=trial_sharpes)
                
                res = {
                    "param_combo": combo,
                    "metrics": stat,
                    "p_value": stat.get("P-Value", task_res.get("p_value")),
                    "significant": (stat.get("DSR P-Value", 1.0) <= 0.05) if "DSR P-Value" in stat else task_res.get("significant", False),
                    "_iteration": i + 1,
                    "_total": total_combinations
                }
                if "error" in task_res:
                    res["error"] = task_res["error"]
                    
                yield json.dumps(res) + "\n"
                    
        except Exception as e:
            yield json.dumps({"error": str(e)}) + "\n"

    return Response(generate(), mimetype='application/jsonl')


@app.route('/api/walk_forward', methods=['POST'])
def run_walk_forward():
    req = request.json or {}
    script_path = req.get('script')
    data_path = req.get('data')
    base_params = req.get('base_params', {})
    optimizable_params = req.get('optimizable_params', [])
    
    wfo_mode = req.get('wfo_mode', 'Rolling')  # 'Rolling' or 'Anchored'
    train_quarters = int(req.get('train_quarters', 8))  # default 8 quarters = 2 years
    test_quarters = int(req.get('test_quarters', 2))    # default 2 quarters = 6 months
    step_quarters = int(req.get('step_quarters', 2))    # default 2 quarters = 6 months
    objective_metric = str(req.get('objective_metric', 'sharpe')).lower()
    
    start_q = req.get('start_quarter')
    end_q = req.get('end_quarter')
    
    if not script_path or not data_path:
        return jsonify({"error": "Missing script or data path"}), 400
    if not optimizable_params:
        return jsonify({"error": "No optimizable parameters specified for Walk-Forward training"}), 400
        
    try:
        module = load_script(script_path)
        q_info = get_dataset_quarter_range(data_path)
        all_dataset_quarters = q_info.get('quarters', [])
        
        # Filter quarters by start_q and end_q if provided
        if start_q and end_q and start_q in all_dataset_quarters and end_q in all_dataset_quarters:
            s_idx = all_dataset_quarters.index(start_q)
            e_idx = all_dataset_quarters.index(end_q)
            quarters = all_dataset_quarters[s_idx : e_idx + 1]
        else:
            quarters = all_dataset_quarters
            
        if len(quarters) < (train_quarters + test_quarters):
            return jsonify({
                "error": f"Not enough quarters ({len(quarters)}) for train ({train_quarters}) + test ({test_quarters}). Need at least {train_quarters + test_quarters} quarters."
            }), 400

        # Build WFO windows
        wfo_windows = []
        c = 0
        while True:
            if wfo_mode == 'Anchored':
                t_start_idx = 0
                t_end_idx = train_quarters - 1 + (c * step_quarters)
            else: # Rolling
                t_start_idx = c * step_quarters
                t_end_idx = t_start_idx + train_quarters - 1
                
            test_start_idx = t_end_idx + 1
            test_end_idx = test_start_idx + test_quarters - 1
            
            if test_start_idx >= len(quarters):
                break
            if test_end_idx >= len(quarters):
                test_end_idx = len(quarters) - 1
                
            wfo_windows.append({
                "cycle": c + 1,
                "train_start_q": quarters[t_start_idx],
                "train_end_q": quarters[t_end_idx],
                "test_start_q": quarters[test_start_idx],
                "test_end_q": quarters[test_end_idx],
                "train_label": f"{quarters[t_start_idx]} to {quarters[t_end_idx]}",
                "test_label": f"{quarters[test_start_idx]} to {quarters[test_end_idx]}"
            })
            
            if test_end_idx == len(quarters) - 1:
                break
            c += 1

        if not wfo_windows:
            return jsonify({"error": "Failed to construct valid Walk-Forward windows."}), 400

        # Generate parameter combinations for grid search
        param_ranges = {}
        for p in optimizable_params:
            name = p.get('name')
            start = float(p.get('start', 0))
            end = float(p.get('end', 0))
            step = float(p.get('step', 0))
            if step <= 0: return jsonify({"error": f"Invalid step size {step} for {name}"}), 400
            is_int = step.is_integer() and start.is_integer()
            current_val = start
            vals = []
            while current_val <= end + (step * 0.0001):
                val_to_use = int(round(current_val)) if is_int else round(current_val, 4)
                vals.append(val_to_use)
                current_val += step
            param_ranges[name] = vals

        keys, values = zip(*param_ranges.items())
        combos = [dict(zip(keys, v)) for v in itertools.product(*values)]
        
        if len(combos) > 2000:
            return jsonify({"error": f"Too many parameter combinations ({len(combos)}). Maximum allowed per WFO window is 2000."}), 400

        # Generator for streaming JSONL execution
        def generate_wfo():
            try:
                # Preload full prepared dataset in memory for speed (retaining prior history for warmup)
                full_prepared_df = prepare_filtered_data(module, data_path)
                if full_prepared_df is None or full_prepared_df.empty:
                    full_prepared_df = prepare_filtered_data(module, data_path, quarters[0], quarters[-1])
                if full_prepared_df is not None and not full_prepared_df.empty:
                    full_prepared_df = full_prepared_df.reset_index(drop=True)
                    
                starting_capital = 100000.0
                rf_annual = float(base_params.get('RISK_FREE_RATE_PCT', 4.0)) / 100.0
                
                all_stitched_oos_trades = []
                cycle_results = []
                total_windows = len(wfo_windows)
                
                for w in wfo_windows:
                    c_num = w["cycle"]
                    # 1. Slice Training Data (IS) with Strategy-Defined Warmup
                    start_dt, _ = quarter_to_dates(w["train_start_q"])
                    _, end_dt = quarter_to_dates(w["train_end_q"])
                    st_date = pd.to_datetime(start_dt).date()
                    end_date = pd.to_datetime(end_dt).date()
                    
                    train_mask = (full_prepared_df['date'] >= st_date) & (full_prepared_df['date'] <= end_date)
                    train_indices = full_prepared_df.index[train_mask]
                    if len(train_indices) == 0:
                        continue
                        
                    train_start_idx = train_indices[0]
                    train_end_idx = train_indices[-1]
                    train_warmup_bars = worker_engine.get_strategy_warmup_bars(module, combos[0] if combos else base_params)
                    train_warmup_start_idx = max(0, train_start_idx - train_warmup_bars)
                    
                    train_buffered_df = full_prepared_df.iloc[train_warmup_start_idx : train_end_idx + 1].copy()
                    
                    # 2. Run Multi-Core Grid Search strictly on Train Window (IS)
                    best_score = -float('inf')
                    best_combo = None
                    best_is_metrics = {}
                    best_is_trades = []
                    trial_sharpes = []
                    trial_ledger_entries = []
                    
                    if len(combos) > 1:
                        with ProcessPoolExecutor(max_workers=min(len(combos), MAX_WORKERS)) as executor:
                            futures = [
                                executor.submit(worker_engine.evaluate_backtest_task, script_path, train_buffered_df, combo, base_params, starting_capital, rf_annual, st_date)
                                for combo in combos
                            ]
                            for f in futures:
                                task_res = f.result()
                                stat = task_res.get("metrics", {})
                                combo = task_res.get("param_combo")
                                t_count = task_res.get("trades_count", 0)
                                
                                t_status = task_res.get("status", "VALID" if (t_count > 0 and "Sharpe" in stat) else ("NO_TRADES" if t_count == 0 else "EXECUTION_ERROR"))
                                if "error" in task_res and t_status == "VALID":
                                    t_status = "EXECUTION_ERROR"

                                if t_count > 0 and "Sharpe" in stat and stat["Sharpe"] is not None and t_status == "VALID":
                                    sr = float(stat["Sharpe"])
                                    trial_sharpes.append(sr)
                                    trial_ledger_entries.append({
                                        "params": combo,
                                        "sharpe": sr,
                                        "trades": t_count,
                                        "status": "VALID",
                                        "selected": False
                                    })
                                else:
                                    trial_ledger_entries.append({
                                        "params": combo,
                                        "sharpe": None,
                                        "trades": t_count,
                                        "status": t_status,
                                        "error": task_res.get("error"),
                                        "selected": False
                                    })
                                
                                if t_status != "VALID" or not t_count:
                                    score = -1000.0
                                else:
                                    # Score calculation based on objective_metric
                                    if objective_metric == "sharpe":
                                        score = float(stat.get("Sharpe", -10.0))
                                    elif objective_metric in ["net_pnl", "profit"]:
                                        score = float(stat.get("Net Profit", stat.get("Total_PnL", -1e6)))
                                    elif objective_metric == "calmar":
                                        score = float(stat.get("Calmar Ratio", stat.get("Calmar_Ratio", -10.0)))
                                    elif objective_metric == "profit_factor":
                                        score = float(stat.get("Profit Factor", stat.get("Profit_Factor", 0.0)))
                                    elif objective_metric == "expectancy":
                                        score = float(stat.get("Expectancy ($)", stat.get("Expectancy", -1000.0)))
                                    else: # composite
                                        sh = max(-3.0, min(float(stat.get("Sharpe", 0.0)), 5.0))
                                        cl = max(-3.0, min(float(stat.get("Calmar Ratio", 0.0)), 10.0))
                                        pf = max(0.0, min(float(stat.get("Profit Factor", 0.0)), 5.0))
                                        exp = max(-500.0, min(float(stat.get("Expectancy ($)", 0.0)), 1000.0)) / 100.0
                                        score = (0.35 * sh) + (0.25 * cl) + (0.20 * pf) + (0.20 * exp)
                                        
                                if score > best_score:
                                    best_score = score
                                    best_combo = combo
                                    best_is_metrics = stat
                                    best_is_trades = task_res.get("trades", [])

                        # Fail-loud check: If 0 parameter combinations produced valid backtest executions, abort WFA cycle
                        valid_is_trials = [e for e in trial_ledger_entries if e.get("status") == "VALID"]
                        if not valid_is_trials and not best_is_trades:
                            raise RuntimeError(
                                f"WFA Cycle {c_num} Aborted: 0 of {len(combos)} parameter combinations produced valid backtest executions. "
                                f"All trials failed with contract violations or execution errors."
                            )
                        if best_combo is None:
                            raise RuntimeError(
                                f"WFA Cycle {c_num} Aborted: No parameter combination achieved a valid scoring threshold."
                            )
                    else:
                        task_res = worker_engine.evaluate_backtest_task(script_path, train_buffered_df, combos[0], base_params, starting_capital, rf_annual, st_date)
                        best_combo = combos[0]
                        best_is_metrics = task_res.get("metrics", {})
                        best_is_trades = task_res.get("trades", [])
                        t_count = task_res.get("trades_count", 0)
                        t_status = task_res.get("status", "VALID" if (t_count > 0 and "Sharpe" in best_is_metrics) else ("NO_TRADES" if t_count == 0 else "EXECUTION_ERROR"))
                        if t_count > 0 and "Sharpe" in best_is_metrics and best_is_metrics["Sharpe"] is not None and t_status == "VALID":
                            sr = float(best_is_metrics["Sharpe"])
                            trial_sharpes.append(sr)
                            trial_ledger_entries.append({
                                "params": combos[0],
                                "sharpe": sr,
                                "trades": t_count,
                                "status": "VALID",
                                "selected": True
                            })
                        else:
                            trial_ledger_entries.append({
                                "params": combos[0],
                                "sharpe": None,
                                "trades": t_count,
                                "status": t_status,
                                "error": task_res.get("error"),
                                "selected": True
                            })


                    # Mark selected winner in trial ledger
                    for entry in trial_ledger_entries:
                        if entry["params"] == best_combo:
                            entry["selected"] = True


                    # Recalculate In-Sample winner metrics with trial Sharpe vector for DSR
                    if best_is_trades:
                        best_is_df = pd.DataFrame(best_is_trades)
                        best_is_df['Dollar_PnL'] = best_is_df['net_pnl']
                        best_is_df['EntryTime'] = to_utc_from_et(best_is_df['entry_time'])
                        best_is_df['ExitTime'] = to_utc_from_et(best_is_df['exit_time'])
                        best_is_df['Commission'] = best_is_df.get('commission', 0.0)
                        best_is_metrics = quant_metrics.calculate_stats(
                            best_is_df, starting_capital, rf_annual, trials_sr_list=trial_sharpes
                        )

                    is_sharpe_obj = (objective_metric == "sharpe")
                    exp_max_sr, sr_std, k_count = quant_metrics.calculate_expected_max_sharpe(trials_sr_list=trial_sharpes)
                    cycle_trial_ledger = {
                        "objective_metric": objective_metric,
                        "selection_mode": "SELECTION_ADJUSTED" if is_sharpe_obj else "INFORMATIONAL_NON_SHARPE_OBJECTIVE",
                        "trials_evaluated": len(combos),
                        "trials_with_valid_sharpe": len(trial_sharpes),
                        "trial_sharpe_std": round(sr_std, 2),
                        "expected_max_sr": round(exp_max_sr, 2),
                        "selected_sharpe": float(best_is_metrics.get("Sharpe", 0.0)) if best_is_metrics.get("Sharpe") is not None else None,
                        "selected_params": best_combo,
                        "raw_k_assumption": "Conservative upper bound assuming independent trials",
                        "trials": trial_ledger_entries
                    }

                            
                    # 3. FREEZE Best Parameters and Slice Test Window (OOS) with Dynamic Warmup Buffer
                    frozen_params = base_params.copy()
                    frozen_params.update(best_combo)
                    
                    test_s_dt, _ = quarter_to_dates(w["test_start_q"])

                    _, test_e_dt = quarter_to_dates(w["test_end_q"])
                    t_st_date = pd.to_datetime(test_s_dt).date()
                    t_end_date = pd.to_datetime(test_e_dt).date()
                    
                    test_mask = (full_prepared_df['date'] >= t_st_date) & (full_prepared_df['date'] <= t_end_date)
                    test_indices = full_prepared_df.index[test_mask]
                    if len(test_indices) == 0:
                        continue
                        
                    test_start_idx = test_indices[0]
                    test_end_idx = test_indices[-1]
                    oos_warmup_bars = worker_engine.get_strategy_warmup_bars(module, frozen_params)
                    oos_warmup_start_idx = max(0, test_start_idx - oos_warmup_bars)
                    
                    oos_buffered_df = full_prepared_df.iloc[oos_warmup_start_idx : test_end_idx + 1].copy()
                    
                    # 4. Execute Frozen Parameters on Buffered OOS Frame
                    oos_res = module.run_backtest(oos_buffered_df, frozen_params)
                    raw_oos_trades = oos_res.get("trades", [])
                    
                    # Strict Filter: Discard warmup-originated trades and enforce 100% boundary flatness
                    oos_trades = []
                    for t in raw_oos_trades:
                        entry_val = t.get('entry_time')
                        exit_val = t.get('exit_time')
                        if entry_val:
                            entry_d = pd.to_datetime(entry_val).date()
                            exit_d = pd.to_datetime(exit_val).date() if exit_val else entry_d
                            if entry_d < t_st_date:
                                # Warmup trade: Enforce that it exited strictly before OOS horizon
                                if exit_d >= t_st_date:
                                    raise ValueError(
                                        f"Boundary Flatness Violation: Warmup trade entered at {entry_val} "
                                        f"remained open into Out-of-Sample window {t_st_date} (exited at {exit_val}). "
                                        f"Strategies must enter Out-of-Sample evaluation 100% FLAT."
                                    )
                                continue
                            oos_trades.append(t)

                    
                    if oos_trades:
                        oos_tdf = pd.DataFrame(oos_trades)
                        oos_tdf['Dollar_PnL'] = oos_tdf['net_pnl']
                        oos_tdf['EntryTime'] = to_utc_from_et(oos_tdf['entry_time'])
                        oos_tdf['ExitTime'] = to_utc_from_et(oos_tdf['exit_time'])
                        oos_tdf['Commission'] = oos_tdf.get('commission', 0.0)
                        oos_stat = quant_metrics.calculate_stats(oos_tdf, starting_capital, rf_annual)
                        all_stitched_oos_trades.extend(oos_trades)
                    else:
                        oos_stat = {
                            "Net Profit": 0.0, "Total_PnL": 0.0, "CAGR (%)": 0.0, "Sharpe": 0.0,
                            "Profit Factor": 0.0, "Winning (%)": 0.0, "Max Drawdown": 0.0, "Trades": 0
                        }
                        
                    # Calculate Window WFE
                    is_cagr = float(best_is_metrics.get("CAGR (%)", best_is_metrics.get("CAGR", 0.0)))
                    oos_cagr = float(oos_stat.get("CAGR (%)", oos_stat.get("CAGR", 0.0)))
                    window_wfe_cagr = (oos_cagr / is_cagr * 100.0) if is_cagr > 0 else (100.0 if (is_cagr == 0 and oos_cagr >= 0) else 0.0)
                    
                    is_sharpe = float(best_is_metrics.get("Sharpe", 0.0))
                    oos_sharpe = float(oos_stat.get("Sharpe", 0.0))
                    window_wfe_sharpe = (oos_sharpe / is_sharpe * 100.0) if is_sharpe > 0 else 0.0
                    
                    cycle_payload = {
                        "type": "cycle_update",
                        "cycle": c_num,
                        "total_cycles": total_windows,
                        "train_window": w["train_label"],
                        "test_window": w["test_label"],
                        "frozen_params": best_combo,
                        "is_metrics": best_is_metrics,
                        "oos_metrics": oos_stat,
                        "trial_ledger": cycle_trial_ledger,
                        "window_wfe_cagr": round(window_wfe_cagr, 1),
                        "window_wfe_sharpe": round(window_wfe_sharpe, 1)
                    }

                    cycle_results.append(cycle_payload)
                    yield json.dumps(cycle_payload) + "\n"
                    
                # 5. Build Final Stitched OOS Analytics
                if all_stitched_oos_trades:
                    stitched_df = pd.DataFrame(all_stitched_oos_trades)
                    stitched_df['Dollar_PnL'] = stitched_df['net_pnl']
                    stitched_df['EntryTime'] = to_utc_from_et(stitched_df['entry_time'])
                    stitched_df['ExitTime'] = to_utc_from_et(stitched_df['exit_time'])
                    stitched_df['Commission'] = stitched_df.get('commission', 0.0)
                    stitched_df = stitched_df.sort_values('ExitTime').reset_index(drop=True)
                    stitched_df['Cumulative_PnL'] = stitched_df['Dollar_PnL'].cumsum()
                    
                    overall_oos_stats = quant_metrics.calculate_stats(stitched_df, starting_capital, rf_annual)
                    
                    pct = (stitched_df['Cumulative_PnL'] / starting_capital) * 100
                    peak = pct.cummax()
                    dd = pct - peak
                    stitched_equity_curve = {
                        "pct": pct.round(2).tolist(),
                        "dd": dd.round(2).tolist(),
                        "dates": stitched_df['ExitTime'].dt.strftime('%Y-%m-%d %H:%M').tolist()
                    }
                else:
                    overall_oos_stats = {}
                    stitched_equity_curve = {}
                    
                # Calculate Multi-version Overall WFE
                avg_is_cagr = np.mean([float(c["is_metrics"].get("CAGR (%)", c["is_metrics"].get("CAGR", 0.0))) for c in cycle_results]) if cycle_results else 0.0
                avg_is_sharpe = np.mean([float(c["is_metrics"].get("Sharpe", 0.0)) for c in cycle_results]) if cycle_results else 0.0
                
                overall_oos_cagr = float(overall_oos_stats.get("CAGR (%)", overall_oos_stats.get("CAGR", 0.0)))
                overall_oos_sharpe = float(overall_oos_stats.get("Sharpe", 0.0))
                
                overall_wfe_cagr = (overall_oos_cagr / avg_is_cagr * 100.0) if avg_is_cagr > 0 else 0.0
                overall_wfe_sharpe = (overall_oos_sharpe / avg_is_sharpe * 100.0) if avg_is_sharpe > 0 else 0.0
                
                pct_profitable_windows = float(np.mean([float(c["oos_metrics"].get("Net Profit", c["oos_metrics"].get("Total_PnL", 0.0))) > 0 for c in cycle_results]) * 100.0) if cycle_results else 0.0
                
                # Parameter Stability calculation: % of windows where parameter remained within +/-25% of previous window
                stability_scores = []
                for p_name in param_ranges.keys():
                    p_vals = [c["frozen_params"].get(p_name) for c in cycle_results if p_name in c["frozen_params"]]
                    if len(p_vals) > 1:
                        jumps = [abs(p_vals[k] - p_vals[k-1]) / (abs(p_vals[k-1]) + 1e-6) for k in range(1, len(p_vals))]
                        stability_scores.append(float(np.mean([j <= 0.25 for j in jumps]) * 100.0))
                param_stability_pct = round(float(np.mean(stability_scores)), 1) if stability_scores else 100.0
                
                final_summary = {
                    "type": "final_summary",
                    "wfo_mode": wfo_mode,
                    "total_windows": total_windows,
                    "overall_oos_metrics": overall_oos_stats,
                    "stitched_equity_curve": stitched_equity_curve,
                    "wfe_cagr": round(overall_wfe_cagr, 1),
                    "wfe_sharpe": round(overall_wfe_sharpe, 1),
                    "pct_profitable_windows": round(pct_profitable_windows, 1),
                    "param_stability_pct": param_stability_pct,
                    "cycles": cycle_results
                }
                yield json.dumps(final_summary) + "\n"
                
            except Exception as e:
                import traceback
                yield json.dumps({"error": str(e), "trace": traceback.format_exc()}) + "\n"
                
        return Response(generate_wfo(), mimetype='application/jsonl')
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/permutation_test', methods=['POST'])
def run_permutation_test():
    req = request.json
    script_path = req.get('script')
    data_path = req.get('data')
    params = req.get('params', {})
    n_sims = req.get('n_sims', 1000)
    
    if not script_path or not data_path:
        return jsonify({"error": "Missing script or data path"}), 400
        
    try:
        module = load_script(script_path)
        
        # Check if the script supports native data permutation
        if hasattr(module, 'run_data_permutation'):
            perm_res = module.run_data_permutation(data_path, params, n_sims=n_sims)
            return jsonify(perm_res)
        else:
            return jsonify({"error": "Strategy script does not support run_data_permutation(). Please implement it in your script."}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/multi_market', methods=['POST'])
def run_multi_market():
    req = request.json
    script_path = req.get('script')
    data_paths = req.get('data_paths', [])
    params = req.get('params', {})
    
    if not script_path or not data_paths or len(data_paths) < 2:
        return jsonify({"error": "Need a script and at least 2 data sources."}), 400
        
    try:
        module = load_script(script_path)
        results = []
        
        # Group paths by symbol
        grouped_paths = {}
        for p in data_paths:
            if isinstance(p, list):
                if len(p) > 0:
                    sym = p[0].split('\\')[-1].split('/')[-1].split('_')[0].upper()
                    grouped_paths[sym] = grouped_paths.get(sym, []) + p
            else:
                sym = p.split('\\')[-1].split('/')[-1].split('_')[0].upper()
                grouped_paths[sym] = grouped_paths.get(sym, []) + [p]
                
        # Now run backtest for each group
        trades_by_symbol = {}  # cache trades DF per symbol, reused below for
                                # drawdown correlation instead of re-running
                                # the backtest a second time
        for sym, dpath_list in grouped_paths.items():
            # Deduplicate the paths to prevent double-counting data
            unique_dpaths = list(dict.fromkeys(dpath_list))
            raw_res = module.run_backtest(unique_dpaths, params)
            if "error" in raw_res:
                continue
                
            trades_list = raw_res.get("trades", [])
            df = pd.DataFrame(trades_list)
            if df.empty:
                continue
                
            df['Dollar_PnL'] = df['net_pnl']
            df['EntryTime'] = to_utc_from_et(df['entry_time'])
            df['ExitTime'] = to_utc_from_et(df['exit_time'])
            df['Commission'] = df.get('commission', 0.0)
            trades_by_symbol[sym] = df[['ExitTime', 'net_pnl']].copy()
            
            rf_annual = float(params.get('RISK_FREE_RATE_PCT', 4.0)) / 100.0
            stats = quant_metrics.calculate_stats(df, 100000.0, rf_annual)
            
            # Group by year
            df['Year'] = df['ExitTime'].dt.year
            yearly_stats = []
            for yr, group in df.groupby('Year'):
                wins = group[group['Dollar_PnL'] > 0]
                losses = group[group['Dollar_PnL'] <= 0]
                wr = len(wins) / len(group) * 100 if len(group) > 0 else 0
                gross_profit = wins['Dollar_PnL'].sum()
                gross_loss = abs(losses['Dollar_PnL'].sum())
                pf = gross_profit / gross_loss if gross_loss != 0 else float('inf')
                
                # Approximate max DD for the year using cumulative sum of trades
                cum_pnl = group['Dollar_PnL'].cumsum()
                peak = cum_pnl.cummax()
                dd = (cum_pnl - peak).min()
                
                # Yearly Long/Short PF
                l_grp = group[group['direction'] == 'long']
                s_grp = group[group['direction'] == 'short']
                l_w = l_grp[l_grp['Dollar_PnL'] > 0]['Dollar_PnL'].sum()
                l_l = abs(l_grp[l_grp['Dollar_PnL'] <= 0]['Dollar_PnL'].sum())
                s_w = s_grp[s_grp['Dollar_PnL'] > 0]['Dollar_PnL'].sum()
                s_l = abs(s_grp[s_grp['Dollar_PnL'] <= 0]['Dollar_PnL'].sum())
                l_pf_y = l_w / l_l if l_l != 0 else float('inf')
                s_pf_y = s_w / s_l if s_l != 0 else float('inf')
                
                yearly_stats.append({
                    "Year": int(yr),
                    "Trades": len(group),
                    "Return (%)": float(group['Dollar_PnL'].sum()),
                    "Max DD (%)": float(dd),
                    "PF": round(float(pf), 2),
                    "PF (L)": round(l_pf_y, 2) if l_pf_y != float('inf') else 'Inf',
                    "PF (S)": round(s_pf_y, 2) if s_pf_y != float('inf') else 'Inf'
                })
                
            # Long/Short PF
            l_trades = df[df['direction'] == 'long']
            s_trades = df[df['direction'] == 'short']
            l_win = l_trades[l_trades['Dollar_PnL'] > 0]['Dollar_PnL'].sum()
            l_loss = abs(l_trades[l_trades['Dollar_PnL'] <= 0]['Dollar_PnL'].sum())
            s_win = s_trades[s_trades['Dollar_PnL'] > 0]['Dollar_PnL'].sum()
            s_loss = abs(s_trades[s_trades['Dollar_PnL'] <= 0]['Dollar_PnL'].sum())
            l_pf = l_win / l_loss if l_loss != 0 else float('inf')
            s_pf = s_win / s_loss if s_loss != 0 else float('inf')
                
            market_name = sym
                
            results.append({
                "market": market_name,
                "metrics": {
                    "Trades": len(df),
                    "Return (%)": f"{round(stats.get('Return (%)', 0), 2)}%",
                    "Max DD (%)": f"{round(stats.get('Max DD (%)', 0), 2)}%",
                    "CAGR (%)": f"{round(stats.get('CAGR (%)', 0), 2)}%",
                    "RoMD": round(stats.get("RoMD", 0), 2),
                    "Profit Factor": round(stats.get('Profit Factor', 0), 2),
                    "Sharpe": round(stats.get('Sharpe', 0), 2),
                    "Sortino": round(stats.get('Sortino', 0), 2),
                    "P-Value": f"{round(stats.get('P-Value', 1.0) * 100, 2)}%" if isinstance(stats.get('P-Value'), (int, float)) else stats.get('P-Value')
                },
                "yearly": yearly_stats
            })
            
        # Drawdown Correlation -- reuses the trades already computed in the
        # per-symbol loop above instead of re-running run_backtest a second
        # time for the same two markets.
        if len(results) >= 2:
            try:
                curves = []
                for sym in list(trades_by_symbol.keys())[:2]:
                    df = trades_by_symbol[sym]
                    if not df.empty:
                        daily = df.groupby(df['ExitTime'].dt.date)['net_pnl'].sum().reset_index()
                        daily.columns = ['Date', 'PnL']
                        daily.set_index('Date', inplace=True)
                        curves.append(daily)
                if len(curves) == 2:
                    merged = pd.merge(curves[0], curves[1], left_index=True, right_index=True, how='outer').fillna(0)
                    merged['Eq1'] = merged['PnL_x'].cumsum()
                    merged['Eq2'] = merged['PnL_y'].cumsum()
                    merged['DD1'] = merged['Eq1'] - merged['Eq1'].cummax()
                    merged['DD2'] = merged['Eq2'] - merged['Eq2'].cummax()
                    corr = merged['DD1'].corr(merged['DD2'])
                    results[0]["dd_corr"] = round(corr, 2)
            except Exception as e:
                pass
                
        return jsonify(results)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/month_calendar', methods=['GET'])
def month_calendar():
    global last_df
    if last_df is None or last_df.empty:
        return jsonify({"error": "No trades available. Please run backtest first."}), 400
        
    year = request.args.get('year')
    month = request.args.get('month')
    
    if not year or not month:
        return jsonify({"error": "Missing year or month"}), 400
        
    starting_capital = 100000.0 # Standard from run_backtest
    try:
        stats = quant_metrics.get_monthly_calendar_stats(last_df, year, month, starting_capital)
        return jsonify(stats)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/day_chart', methods=['POST'])
def day_chart():
    req = request.json
    date_str = req.get('date')
    data_path = req.get('data_path')
    
    if not date_str or not data_path:
        return jsonify({"error": "Missing date or data path"}), 400
        
    try:
        paths = data_path if isinstance(data_path, list) else [data_path]
        day_df = pd.DataFrame()
        target_date = pd.to_datetime(date_str).date()
        
        for p in paths:
            if p.endswith('.parquet'):
                raw_df = pd.read_parquet(p)
            else:
                raw_df = pd.read_csv(p)
            
            df = normalize_databento_data(raw_df)
            df['DateTime'] = df['ts_event']
            df['DateTime_ET'] = df['DateTime'].dt.tz_convert('US/Eastern')
            
            start_et = pd.Timestamp(target_date) - pd.Timedelta(days=1) + pd.Timedelta(hours=18)
            end_et = pd.Timestamp(target_date) + pd.Timedelta(hours=17, minutes=59, seconds=59)
            start_et = start_et.tz_localize('US/Eastern')
            end_et = end_et.tz_localize('US/Eastern')
            
            temp_df = df[(df['DateTime_ET'] >= start_et) & (df['DateTime_ET'] <= end_et)].copy()
            if not temp_df.empty:
                day_df = temp_df
                break

        
        if day_df.empty:
            return jsonify({"error": "No market data found for this date"}), 404
            
        candles = []
        for _, row in day_df.iterrows():
            c = {
                "time": int(row['DateTime'].timestamp()),
                "o": float(row.get('Open', row.get('open', 0))),
                "h": float(row.get('High', row.get('high', 0))),
                "l": float(row.get('Low', row.get('low', 0))),
                "c": float(row.get('Close', row.get('close', 0)))
            }
            if 'Volume' in row or 'volume' in row: 
                c['v'] = float(row.get('Volume', row.get('volume', 0)))
            if 'Delta' in row: c['d'] = float(row['Delta'])
            if 'VWAP' in row: c['vwap'] = float(row['VWAP'])
            candles.append(c)
            
        global last_df
        trades = []
        if last_df is not None and not last_df.empty:
            day_trades = last_df[(last_df['EntryTime'].dt.date == target_date) | (last_df['ExitTime'].dt.date == target_date)]
            for _, t in day_trades.iterrows():
                trades.append({
                    "type": "Long" if str(t.get('Type', t.get('direction', ''))).lower() == 'long' else "Short",
                    "entry_time": int(t['EntryTime'].timestamp()),
                    "exit_time": int(t['ExitTime'].timestamp()),
                    "entry_price": float(t.get('EntryPrice', t.get('entry_price', 0))),
                    "exit_price": float(t.get('ExitPrice', t.get('exit_price', 0))),
                    "pnl": float(t.get('Dollar_PnL', t.get('net_pnl', 0))),
                    "reason": t.get('ExitReason', t.get('exit_reason', 'Exit')),
                    "tp": float(t.get('TP', t.get('tp', t.get('PT', t.get('pt', 0))))),
                    "sl": float(t.get('SL', t.get('sl', 0))),
                    "qty": int(t.get('Qty', t.get('qty', 1)))
                })
                
        return jsonify({
            "candles": candles,
            "trades": trades
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True, port=5050)
