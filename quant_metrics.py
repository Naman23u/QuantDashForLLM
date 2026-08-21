import pandas as pd
import numpy as np
from datetime import datetime
import math
from numba import njit, prange
import scipy.special

EULER_MASCHERONI = 0.57721566490153286060

def safe_round(val, ndigits=2):
    """round() that passes None through instead of raising TypeError.
    Used for ratios whose denominator can legitimately be zero (e.g. Profit
    Factor / RoMD / Calmar Ratio with no losing trades or no drawdown yet) --
    those return None (-> JSON null -> frontend shows 'N/A') rather than
    float('inf'), which is not valid JSON and breaks JSON.parse() on the
    entire response, not just that one field.
    """
    return None if val is None else round(val, ndigits)

try:
    import pandas_market_calendars as mcal
    _CME_CALENDAR = mcal.get_calendar('CME_Equity')
except Exception:
    _CME_CALENDAR = None

def _get_trading_days(start_date, end_date):
    """
    Actual CME futures trading days between start_date and end_date
    (inclusive) -- excludes weekends AND market holidays (Thanksgiving,
    Christmas, Good Friday, etc.), unlike pandas' freq='B' which only
    excludes weekends and silently counts holidays as ordinary flat/zero-
    return trading days. That inflates the day-count denominator used for
    Sharpe/Sortino's business-day reindexing and for Exposure % / Daily
    Trade Frequency by roughly 8-10 phantom days a year.

    Falls back to freq='B' (weekends-only) if pandas_market_calendars isn't
    installed, so this never hard-crashes a backtest over a missing package.
    """
    if _CME_CALENDAR is not None:
        schedule = _CME_CALENDAR.schedule(start_date=start_date, end_date=end_date)
        return schedule.index.date
    return pd.date_range(start=start_date, end=end_date, freq='B').date


def calculate_expected_max_sharpe(trials_sr_list=None, n_trials=1, sr_std=None):
    """
    Computes the expected maximum Sharpe ratio under the null hypothesis of zero true alpha
    across K trials (Bailey & López de Prado, 2014).
    
    Formula:
        SR* = sqrt(Var(SR)) * [(1 - γ) * Φ^(-1)(1 - 1/K) + γ * Φ^(-1)(1 - 1/(K*e))]
        where γ is Euler-Mascheroni constant and e is Euler's number.
    
    Returns:
        tuple: (expected_max_sr, sr_std, k_trials)
    """
    if trials_sr_list is not None:
        clean_sr = [float(s) for s in trials_sr_list if pd.notna(s) and np.isfinite(s)]
        k_trials = len(clean_sr)
        if k_trials <= 1:
            return 0.0, 0.0, max(1, k_trials)
        sr_std = float(np.std(clean_sr, ddof=1))
    else:
        k_trials = int(n_trials) if n_trials is not None else 1
        sr_std = float(sr_std) if sr_std is not None else 0.0

    # Singularity guard: If K <= 1 or no dispersion across trials, SR* hurdle is 0.0
    if k_trials <= 1 or sr_std <= 0.0:
        return 0.0, max(0.0, sr_std), max(1, k_trials)

    # Standardized normal expected extremum Z*
    p1 = 1.0 - (1.0 / float(k_trials))
    p2 = 1.0 - (1.0 / (float(k_trials) * math.e))

    # Numerical probit safety bounds
    p1 = max(1e-15, min(1.0 - 1e-15, p1))
    p2 = max(1e-15, min(1.0 - 1e-15, p2))

    z1 = float(scipy.special.ndtri(p1))
    z2 = float(scipy.special.ndtri(p2))

    expected_z = (1.0 - EULER_MASCHERONI) * z1 + EULER_MASCHERONI * z2
    expected_max_sr = max(0.0, expected_z * sr_std)

    return float(expected_max_sr), float(sr_std), int(k_trials)


def calculate_dsr(observed_sr_annual, df_daily, mean_excess, std_excess, trials_sr_list=None, n_trials=1, sr_std=None):
    """
    Computes the Deflated Sharpe Ratio (DSR) correcting for selection bias,
    multiple testing, and non-normal return distributions (Bailey & López de Prado, 2014).
    """
    expected_max_sr, trial_std, k_trials = calculate_expected_max_sharpe(trials_sr_list, n_trials, sr_std)

    if len(df_daily) < 5 or std_excess <= 0:
        return {
            "DSR (%)": 0.0,
            "DSR P-Value": 1.0,
            "Expected Max SR": round(expected_max_sr, 2),
            "Trial Sharpe Std": round(trial_std, 2),
            "Trials Tested (K)": k_trials
        }

    n_obs = len(df_daily)
    excess_vals = df_daily['Excess_Return'].values
    dev_excess = (excess_vals - mean_excess) / std_excess
    daily_skew = float(np.mean(dev_excess ** 3))
    daily_kurt = float(np.mean(dev_excess ** 4))  # uncentered kurtosis (normal = 3.0)

    # Daily Sharpe per period & de-annualized hurdle
    s_per_period = float(mean_excess / std_excess)
    sr_star_daily = expected_max_sr / math.sqrt(252.0)

    # Asymptotic variance of Sharpe estimator under non-normality
    var_s = (1.0 - daily_skew * s_per_period + ((daily_kurt - 1.0) / 4.0) * (s_per_period ** 2)) / (n_obs - 1)

    if var_s > 0:
        z_dsr = (s_per_period - sr_star_daily) / math.sqrt(var_s)
        # Normal CDF
        dsr_val = 0.5 * (1.0 + math.erf(z_dsr / math.sqrt(2.0)))
        dsr_pct = round(dsr_val * 100.0, 2)
        p_dsr = round(max(0.0, min(1.0, 1.0 - dsr_val)), 4)
    else:
        dsr_pct = 50.0
        p_dsr = 1.0

    return {
        "DSR (%)": dsr_pct,
        "DSR P-Value": p_dsr,
        "Expected Max SR": round(expected_max_sr, 2),
        "Trial Sharpe Std": round(trial_std, 2),
        "Trials Tested (K)": k_trials
    }


def calculate_stats(df, starting_capital=100000.0, rf_annual=0.04, trials_sr_list=None, n_trials=1, sr_std=None):
    if df.empty:
        return {
            "Trades": 0, "Exposure (%)": 0.0, "Daily Trade Frequency": 0.0, "Active Day Trade Frequency": 0.0,
            "Annualized Return": 0.0, "CAGR (%)": 0.0, "Annualized Volatility": 0.0, "Max Drawdown": 0.0,
            "Payoff Ratio": 0.0, "MFE (%)": 0.0, "MAE (%)": 0.0, "Average Losing Trade Duration": 0.0,
            "Average Winning Trade Duration": 0.0, "Percentage of Winning Days": 0.0, "Duration Ratio": 0.0,
            "Average Winning Trade": 0.0, "Average Losing Trade": 0.0, "Profit Factor": 0.0, "Winning (%)": 0.0,
            "Expectancy ($)": 0.0, "Commission Paid": 0.0, "Slippage Paid": 0.0, "Largest Losing Trade": 0.0,
            "Largest Winning Trade": 0.0, "Max Consec Lose": 0, "Max Consec Winning": 0, "Return (%)": 0.0,
            "Max DD (%)": 0.0, "P-Value": "N/A", "PSR (%)": 0.0, "Skewness": 0.0, "Kurtosis": 0.0,
            "DSR (%)": 0.0, "DSR P-Value": 1.0, "Expected Max SR": 0.0, "Trial Sharpe Std": 0.0, "Trials Tested (K)": 1,
            "RoMD": 0.0, "Sharpe": 0.0, "Sortino": 0.0,
            "Top 1%": 0.0, "Top 5%": 0.0, "Top 10%": 0.0, "Max Drawdown ($)": 0.0, "Max Drawdown Duration": 0,
            "Ulcer Index": 0.0, "UPI": 0.0, "Calmar Ratio": 0.0, "K-Ratio": 0.0, "Net Profit": 0.0
        }

        
    wins = df[df['Dollar_PnL'] > 0]
    losses = df[df['Dollar_PnL'] <= 0]
    
    net_profit = float(df['Dollar_PnL'].sum())
    gross_profit = float(wins['Dollar_PnL'].sum()) if not wins.empty else 0.0
    gross_loss = float(losses['Dollar_PnL'].sum()) if not losses.empty else 0.0
    
    cum_pnl = df['Dollar_PnL'].cumsum()
    peak = cum_pnl.cummax()
    drawdown = peak - cum_pnl
    max_dd = float(drawdown.max())
    max_dd_pct = float((max_dd / starting_capital) * 100) if starting_capital else 0.0
    romd = (net_profit / max_dd) if max_dd > 0 else None
    
    pf = float(gross_profit / abs(gross_loss)) if gross_loss != 0 else None
    commissions = float(df['Commission'].sum()) if 'Commission' in df.columns else (float(df['commission'].sum()) if 'commission' in df.columns else 0.0)
    slippage = float(df['slippage'].sum()) if 'slippage' in df.columns else 0.0
    
    total_trades = len(df)
    win_rate = float((len(wins) / total_trades * 100)) if total_trades > 0 else 0.0
    
    avg_win = float(wins['Dollar_PnL'].mean()) if len(wins) > 0 else 0.0
    avg_loss = float(losses['Dollar_PnL'].mean()) if len(losses) > 0 else 0.0
    payoff_ratio = float(avg_win / abs(avg_loss)) if avg_loss != 0 else None
    expectancy = float((avg_win * (win_rate/100)) + (avg_loss * (1 - (win_rate/100))))
    
    largest_win = float(df['Dollar_PnL'].max())
    largest_loss = float(df['Dollar_PnL'].min())
    
    # Consec Win/Loss
    is_win = df['Dollar_PnL'] > 0
    max_cons_wins = int(is_win.groupby((~is_win).cumsum()).sum().max())
    max_cons_losses = int((~is_win).groupby(is_win.cumsum()).sum().max())

    # Time/Duration parsing
    if 'entry_time' in df.columns and 'exit_time' in df.columns:
        entry_dt = pd.to_datetime(df['entry_time'], utc=True)
        exit_dt = pd.to_datetime(df['exit_time'], utc=True)
        durations = (exit_dt - entry_dt).dt.total_seconds() / 60.0
    else:
        durations = pd.Series([0.0]*len(df))
        
    avg_win_duration = float(durations[is_win].mean()) if len(wins) > 0 else 0.0
    avg_lose_duration = float(durations[~is_win].mean()) if len(losses) > 0 else 0.0
    duration_ratio = float(avg_win_duration / avg_lose_duration) if avg_lose_duration > 0 else None

    # MFE / MAE
    if 'direction' in df.columns:
        long_mask = df['direction'] == 'long'
        short_mask = df['direction'] == 'short'
    elif 'Type' in df.columns:
        long_mask = df['Type'] == 'Long'
        short_mask = df['Type'] == 'Short'
    else:
        long_mask = pd.Series([True] * len(df), index=df.index)
        short_mask = pd.Series([False] * len(df), index=df.index)
    mfe_pct = 0.0
    mae_pct = 0.0

    if 'mfe_price' in df.columns and 'mae_price' in df.columns and 'entry_price' in df.columns:
        long_mfe = (df.loc[long_mask, 'mfe_price'] - df.loc[long_mask, 'entry_price']) / df.loc[long_mask, 'entry_price'] * 100
        long_mae = (df.loc[long_mask, 'mae_price'] - df.loc[long_mask, 'entry_price']) / df.loc[long_mask, 'entry_price'] * 100
        short_mfe = (df.loc[short_mask, 'entry_price'] - df.loc[short_mask, 'mfe_price']) / df.loc[short_mask, 'entry_price'] * 100
        short_mae = (df.loc[short_mask, 'entry_price'] - df.loc[short_mask, 'mae_price']) / df.loc[short_mask, 'entry_price'] * 100
        mfe_val = pd.concat([long_mfe, short_mfe]).mean()
        mae_val = pd.concat([long_mae, short_mae]).mean()
        mfe_pct = float(mfe_val) if pd.notna(mfe_val) else 0.0
        mae_pct = float(mae_val) if pd.notna(mae_val) else 0.0

    # Institutional Sharpe and Sortino
    if 'ExitTime' in df.columns and not df.empty:
        df_clean = df.dropna(subset=['ExitTime'])
        if not df_clean.empty:
            start_date = df_clean['ExitTime'].min().date()
            end_date = df_clean['ExitTime'].max().date()
            daily_pnl = df_clean.groupby(df_clean['ExitTime'].dt.date)['Dollar_PnL'].sum()

            all_bday_dates = _get_trading_days(start_date, end_date)
            df_daily = pd.DataFrame(index=all_bday_dates)
            df_daily['Daily_PnL'] = daily_pnl.reindex(df_daily.index, fill_value=0.0)

            df_daily['Return'] = df_daily['Daily_PnL'] / starting_capital
            daily_rf = rf_annual / 252.0
            df_daily['Excess_Return'] = df_daily['Return'] - daily_rf

            mean_excess = df_daily['Excess_Return'].mean()
            std_excess = df_daily['Excess_Return'].std(ddof=1)
            N_days = len(df_daily)
            
            if pd.notna(std_excess) and std_excess > 0 and N_days > 0:
                sharpe = float((mean_excess / std_excess) * np.sqrt(252))
            else:
                sharpe = 0.0

            downside_diff = np.minimum(0, df_daily['Excess_Return'])
            downside_dev = np.sqrt(np.sum(downside_diff**2) / N_days)
            if pd.notna(downside_dev) and downside_dev > 0:
                sortino = float((mean_excess / downside_dev) * np.sqrt(252))
            else:
                sortino = 0.0

            daily_std = float(df_daily['Return'].std(ddof=1)) if len(df_daily) > 1 else 0.0
            annual_vol = float(daily_std * np.sqrt(252)) if pd.notna(daily_std) else 0.0
            
            years = N_days / 252.0
            ending_capital = starting_capital + net_profit
            # The geometric CAGR formula (end/start)^(1/years) requires a
            # non-negative base -- it's mathematically undefined once the
            # account goes to zero or negative (a fractional power of a
            # negative number is complex). In that case, fall back to the
            # simple (non-annualized) signed total return so a blown account
            # shows something like -134.2%, not a misleadingly neutral 0.0%.
            if years > 0 and ending_capital > 0:
                cagr = float(((ending_capital / starting_capital) ** (1 / years) - 1) * 100)
            elif starting_capital > 0:
                cagr = float(((ending_capital / starting_capital) - 1) * 100)
            else:
                cagr = 0.0
            
            win_days = (df_daily['Daily_PnL'] > 0).sum()
            pct_winning_days = float(win_days / len(df_daily) * 100) if len(df_daily) > 0 else 0.0
            
            daily_trade_counts = df_clean.groupby(df_clean['ExitTime'].dt.date).size()
            active_days = len(daily_trade_counts)
            daily_freq = float(total_trades / len(df_daily)) if len(df_daily) > 0 else 0.0
            active_day_freq = float(total_trades / active_days) if active_days > 0 else 0.0
            
            total_trade_mins = durations.sum()
            total_market_mins = len(df_daily) * 390
            exposure_pct = float(total_trade_mins / total_market_mins * 100) if total_market_mins > 0 else 0.0

            # Day-level equity curve, used for drawdown duration and tail-risk
            # metrics below (calendar time, not trade-sequence time, so a
            # multi-day underwater stretch with no trades still counts).
            equity_curve = starting_capital + df_daily['Daily_PnL'].cumsum()
            running_peak = equity_curve.cummax()
            dd_dollars_daily = running_peak - equity_curve
            dd_pct_daily = (dd_dollars_daily / running_peak.replace(0, np.nan)) * 100.0
            dd_pct_daily = dd_pct_daily.fillna(0.0)

            # Max Drawdown Duration: longest consecutive run of underwater
            # days (same run-length-encoding trick used for win/loss streaks)
            is_underwater = dd_dollars_daily > 0
            if is_underwater.any():
                grp = (~is_underwater).cumsum()
                max_dd_duration_days = int(is_underwater.groupby(grp).sum().max())
            else:
                max_dd_duration_days = 0

            # Ulcer Index: RMS of daily % drawdown (penalizes deep AND long
            # drawdowns, unlike Max DD which only captures the single worst point)
            ulcer_index = float(np.sqrt(np.mean(dd_pct_daily.values ** 2))) if len(dd_pct_daily) > 0 else 0.0

            # UPI / Martin Ratio: excess annualized return per unit of Ulcer Index
            upi = float((cagr - rf_annual * 100.0) / ulcer_index) if ulcer_index > 0 else 0.0

            # K-Ratio: OLS regression of the cumulative % return curve against
            # time; slope / standard-error-of-slope. Measures how linear/consistent
            # the equity growth is -- a smooth steady climb scores far higher than
            # the same total return achieved in one lumpy jump.
            cum_ret_curve = (equity_curve - starting_capital) / starting_capital * 100.0
            n_k = len(cum_ret_curve)
            if n_k > 2:
                x_idx = np.arange(n_k, dtype=float)
                slope, intercept = np.polyfit(x_idx, cum_ret_curve.values, 1)
                fitted = slope * x_idx + intercept
                resid = cum_ret_curve.values - fitted
                dof = n_k - 2
                resid_std = np.sqrt(np.sum(resid ** 2) / dof) if dof > 0 else 0.0
                sxx = np.sum((x_idx - x_idx.mean()) ** 2)
                se_slope = resid_std / np.sqrt(sxx) if sxx > 0 else 0.0
                raw_k = float(slope / se_slope) if se_slope > 0 else 0.0
                k_ratio = raw_k * np.sqrt(252.0) / n_k
            else:
                k_ratio = 0.0

        else:
            sharpe, sortino, daily_std, annual_vol, cagr = 0.0, 0.0, 0.0, 0.0, 0.0
            pct_winning_days, daily_freq, active_day_freq, exposure_pct = 0.0, 0.0, 0.0, 0.0
            max_dd_duration_days, ulcer_index, upi, k_ratio = 0, 0.0, 0.0, 0.0
    else:
        sharpe, sortino, daily_std, annual_vol, cagr = 0.0, 0.0, 0.0, 0.0, 0.0
        pct_winning_days, daily_freq, active_day_freq, exposure_pct = 0.0, 0.0, 0.0, 0.0
        max_dd_duration_days, ulcer_index, upi, k_ratio = 0, 0.0, 0.0, 0.0

    # Calmar Ratio: CAGR / |Max Drawdown %|. Conventionally uses a trailing
    # 3-year window; for backtests shorter than 3 years (the common case here)
    # this falls back to the full sample's CAGR and Max DD, same convention
    # most retail/prop tooling uses when 3 years of data isn't available.
    calmar_ratio = float(cagr / max_dd_pct) if max_dd_pct > 0 else None

    return_pct = float((net_profit / starting_capital) * 100)
    ann_return = cagr # Proxy for annualized return

    # Top 1%, 5%, 10% Returns (incurred by the best trades)
    sorted_trades = df['Dollar_PnL'].sort_values(ascending=False)
    n_t = len(sorted_trades)
    
    def get_top_pct_return(pct):
        if n_t == 0: return 0.0
        exclude_count = max(1, int(n_t * (pct / 100.0)))
        remaining_pnl = sorted_trades.iloc[exclude_count:].sum()
        return float((remaining_pnl / starting_capital) * 100)
    
    top_1 = get_top_pct_return(1)
    top_5 = get_top_pct_return(5)
    top_10 = get_top_pct_return(10)
    
    # Probabilistic Sharpe Ratio (PSR) & Fat-Tail Adjusted Significance
    # (Bailey & López de Prado, 2012: The Sharpe Ratio Efficient Frontier)
    psr_pct = 50.0
    p_value = 1.0
    daily_skew = 0.0
    daily_kurt = 3.0
    
    if len(df_daily) >= 5 and std_excess > 0:
        n_obs = len(df_daily)
        excess_vals = df_daily['Excess_Return'].values
        dev_excess = (excess_vals - mean_excess) / std_excess
        daily_skew = float(np.mean(dev_excess ** 3))
        daily_kurt = float(np.mean(dev_excess ** 4))  # uncentered kurtosis (normal = 3.0)
        
        # Daily Sharpe per period
        s_per_period = float(mean_excess / std_excess)
        
        # Asymptotic variance of Sharpe estimator under non-normality
        var_s = (1.0 - daily_skew * s_per_period + ((daily_kurt - 1.0) / 4.0) * (s_per_period ** 2)) / (n_obs - 1)
        
        if var_s > 0:
            z_stat = s_per_period / math.sqrt(var_s)
            # Normal CDF
            psr_val = 0.5 * (1.0 + math.erf(z_stat / math.sqrt(2.0)))
            psr_pct = round(psr_val * 100.0, 2)
            p_val = max(0.0, min(1.0, 1.0 - psr_val))
            p_value = round(p_val, 4)
        else:
            psr_pct = 50.0
            p_value = 1.0
    else:
        p_value = "N/A"
        psr_pct = 0.0

    dsr_results = calculate_dsr(
        observed_sr_annual=sharpe,
        df_daily=df_daily,
        mean_excess=mean_excess,
        std_excess=std_excess,
        trials_sr_list=trials_sr_list,
        n_trials=n_trials,
        sr_std=sr_std
    )


    res = {
        "Trades": total_trades,
        "Exposure (%)": round(exposure_pct, 2),
        "Daily Trade Frequency": round(daily_freq, 2),
        "Active Day Trade Frequency": round(active_day_freq, 2),
        "Annualized Return": round(ann_return, 2),
        "CAGR (%)": round(cagr, 2),
        "Annualized Volatility": round(annual_vol * 100, 2),
        "Max Drawdown": round(max_dd_pct, 2),
        "Payoff Ratio": safe_round(payoff_ratio, 2),
        "MFE (%)": round(mfe_pct, 2),
        "MAE (%)": round(mae_pct, 2),
        "Average Losing Trade Duration": round(avg_lose_duration, 1),
        "Average Winning Trade Duration": round(avg_win_duration, 1),
        "Percentage of Winning Days": round(pct_winning_days, 1),
        "Duration Ratio": safe_round(duration_ratio, 2),
        "Average Winning Trade": round(avg_win, 2),
        "Average Losing Trade": round(avg_loss, 2),
        "Profit Factor": safe_round(pf, 2),
        "Winning (%)": round(win_rate, 2),
        "Expectancy ($)": round(expectancy, 2),
        "Commission Paid": round(commissions, 2),
        "Slippage Paid": round(slippage, 2),
        "Largest Losing Trade": round(largest_loss, 2),
        "Largest Winning Trade": round(largest_win, 2),
        "Max Consec Lose": max_cons_losses,
        "Max Consec Winning": max_cons_wins,
        "Return (%)": round(return_pct, 2),
        "Max DD (%)": round(max_dd_pct, 2),
        "P-Value": p_value,
        "PSR (%)": psr_pct,
        "DSR (%)": dsr_results["DSR (%)"],
        "DSR P-Value": dsr_results["DSR P-Value"],
        "Expected Max SR": dsr_results["Expected Max SR"],
        "Trial Sharpe Std": dsr_results["Trial Sharpe Std"],
        "Trials Tested (K)": dsr_results["Trials Tested (K)"],
        "Skewness": round(daily_skew, 2),
        "Kurtosis": round(daily_kurt, 2),
        "RoMD": safe_round(romd, 2),
        "Sharpe": round(sharpe, 2),
        "Sortino": round(sortino, 2),
        "Top 1%": round(top_1, 2),
        "Top 5%": round(top_5, 2),
        "Top 10%": round(top_10, 2),
        "Max Drawdown ($)": round(max_dd, 2),

        "Max Drawdown Duration": max_dd_duration_days,
        "Ulcer Index": round(ulcer_index, 2),
        "UPI": round(upi, 2),
        "Calmar Ratio": safe_round(calmar_ratio, 2),
        "K-Ratio": round(k_ratio, 2),
        "Net Profit": round(net_profit, 2)
    }

    return res

def estimate_optimal_block_length_politis_white(returns_array):
    """
    Automatic block-length selection for the stationary bootstrap (Politis & White, 2004; 
    Patton, Politis & White, 2009 correction) using flat-top trapezoidal spectral windows.
    """
    X = np.asarray(returns_array, dtype=np.float64)
    N = len(X)
    if N < 10:
        return 3, "FALLBACK_SMALL_SAMPLE"
    mean_x = np.mean(X)
    gamma_0 = float(np.var(X))
    if gamma_0 <= 1e-12:
        return 3, "FALLBACK_ZERO_VARIANCE"
    
    max_lag = min(N - 1, max(10, int(2 * np.sqrt(N))))
    gamma = np.zeros(max_lag + 1)
    for k in range(max_lag + 1):
        gamma[k] = np.sum((X[:N - k] - mean_x) * (X[k:] - mean_x)) / N
    rho = gamma / gamma_0
    
    # Politis & White flat-top window selection criterion
    c = 2.0
    thresh = c * np.sqrt(np.log10(N) / N)
    M = 0
    k_max = min(5, max_lag)
    for m in range(1, max_lag - k_max + 1):
        if np.all(np.abs(rho[m:m + k_max]) < thresh):
            M = m
            break
    if M == 0:
        M = max(1, int(np.ceil(np.sqrt(N))))
    
    # Trapezoidal flat-top lag window lambda(z)
    def flat_top_lag_window(z):
        abs_z = abs(z)
        if abs_z <= 0.5:
            return 1.0
        elif abs_z <= 1.0:
            return 2.0 * (1.0 - abs_z)
        return 0.0
    
    # Spectral moments G and D (Patton, Politis & White 2009 correction)
    G = 0.0
    sum_gamma_weighted = 0.0
    for k in range(-M, M + 1):
        abs_k = abs(k)
        w = flat_top_lag_window(abs_k / float(M))
        sum_gamma_weighted += w * gamma[abs_k]
        G += w * float(abs_k) * gamma[abs_k]
        
    D = 2.0 * (sum_gamma_weighted ** 2)
    
    if D > 1e-12 and G > 1e-12:
        b_sb = ((2.0 * (G ** 2)) / D) ** (1.0 / 3.0) * (float(N) ** (1.0 / 3.0))
        b_opt = max(3, min(int(round(b_sb)), min(50, max(3, N // 4))))
        return int(b_opt), "AUTO_POLITIS_WHITE_INSPIRED"
    else:
        fallback = max(3, min(30, int(round(N ** (1.0 / 3.0)))))
        return int(fallback), "AUTO_FALLBACK_CUBE_ROOT"


@njit(parallel=True)
def _numba_stationary_bootstrap_engine(returns_array, num_sims, n_obs, p_switch, starting_capital, rf_daily, seed):
    sim_returns = np.zeros(num_sims)
    sim_sharpe = np.zeros(num_sims)
    sim_max_dd = np.zeros(num_sims)
    sim_dd_duration = np.zeros(num_sims)
    sim_recovery_time = np.zeros(num_sims)
    sim_unrecovered = np.zeros(num_sims)
    sim_matrix = np.zeros((num_sims, n_obs + 1))
    
    for i in prange(num_sims):
        if seed >= 0:
            np.random.seed(seed + i)
            
        sim_matrix[i, 0] = starting_capital
        
        curr_idx = np.random.randint(0, n_obs)
        current_eq = starting_capital
        peak_eq = starting_capital
        peak_day = 0
        
        max_dd_pct = 0.0
        max_dd_peak_day = 0
        max_dd_trough_day = 0
        max_dd_recovery_day = 0
        in_max_dd_episode = False
        
        sum_ret = 0.0
        sum_sq_ret = 0.0
        
        for t in range(n_obs):
            if t > 0:
                if np.random.random() < p_switch:
                    curr_idx = np.random.randint(0, n_obs)
                else:
                    curr_idx = (curr_idx + 1) % n_obs
                    
            r = returns_array[curr_idx]
            current_eq *= (1.0 + r)
            if current_eq < 0.0:
                current_eq = 0.0
            sim_matrix[i, t + 1] = current_eq
            
            sum_ret += r
            sum_sq_ret += r * r
            
            if current_eq > peak_eq:
                if in_max_dd_episode and max_dd_recovery_day == 0:
                    max_dd_recovery_day = t + 1
                    in_max_dd_episode = False
                peak_eq = current_eq
                peak_day = t + 1
            else:
                if peak_eq > 0.0:
                    dd_pct = ((peak_eq - current_eq) / peak_eq) * 100.0
                else:
                    dd_pct = 100.0
                if dd_pct > max_dd_pct:
                    max_dd_pct = dd_pct
                    max_dd_peak_day = peak_day
                    max_dd_trough_day = t + 1
                    max_dd_recovery_day = 0
                    in_max_dd_episode = True
                    
        sim_returns[i] = ((current_eq - starting_capital) / starting_capital) * 100.0
        sim_max_dd[i] = max_dd_pct
        
        if max_dd_pct > 0.0:
            if max_dd_recovery_day > 0:
                sim_dd_duration[i] = float(max_dd_recovery_day - max_dd_peak_day)
                sim_recovery_time[i] = float(max_dd_recovery_day - max_dd_trough_day)
                sim_unrecovered[i] = 0.0
            else:
                sim_dd_duration[i] = float(n_obs - max_dd_peak_day)
                sim_recovery_time[i] = float(n_obs - max_dd_trough_day)
                sim_unrecovered[i] = 1.0
        else:
            sim_dd_duration[i] = 0.0
            sim_recovery_time[i] = 0.0
            sim_unrecovered[i] = 0.0
            
        mean_r = sum_ret / n_obs
        var_r = (sum_sq_ret - n_obs * (mean_r ** 2)) / max(1, n_obs - 1)
        std_r = np.sqrt(max(0.0, var_r))
        if std_r > 1e-12:
            sim_sharpe[i] = ((mean_r - rf_daily) / std_r) * np.sqrt(252.0)
        else:
            sim_sharpe[i] = 0.0
            
    return sim_matrix, sim_returns, sim_sharpe, sim_max_dd, sim_dd_duration, sim_recovery_time, sim_unrecovered

@njit(parallel=True)
def _numba_iid_bootstrap_engine(returns_array, num_sims, n_obs, starting_capital, rf_daily, seed):
    sim_returns = np.zeros(num_sims)
    sim_sharpe = np.zeros(num_sims)
    sim_max_dd = np.zeros(num_sims)
    sim_dd_duration = np.zeros(num_sims)
    sim_recovery_time = np.zeros(num_sims)
    sim_unrecovered = np.zeros(num_sims)
    sim_matrix = np.zeros((num_sims, n_obs + 1))
    
    for i in prange(num_sims):
        if seed >= 0:
            np.random.seed(seed + i)
            
        sim_matrix[i, 0] = starting_capital
        current_eq = starting_capital
        peak_eq = starting_capital
        peak_day = 0
        
        max_dd_pct = 0.0
        max_dd_peak_day = 0
        max_dd_trough_day = 0
        max_dd_recovery_day = 0
        in_max_dd_episode = False
        
        sum_ret = 0.0
        sum_sq_ret = 0.0
        
        for t in range(n_obs):
            curr_idx = np.random.randint(0, n_obs)
            r = returns_array[curr_idx]
            current_eq *= (1.0 + r)
            if current_eq < 0.0:
                current_eq = 0.0
            sim_matrix[i, t + 1] = current_eq
            
            sum_ret += r
            sum_sq_ret += r * r
            
            if current_eq > peak_eq:
                if in_max_dd_episode and max_dd_recovery_day == 0:
                    max_dd_recovery_day = t + 1
                    in_max_dd_episode = False
                peak_eq = current_eq
                peak_day = t + 1
            else:
                if peak_eq > 0.0:
                    dd_pct = ((peak_eq - current_eq) / peak_eq) * 100.0
                else:
                    dd_pct = 100.0
                if dd_pct > max_dd_pct:
                    max_dd_pct = dd_pct
                    max_dd_peak_day = peak_day
                    max_dd_trough_day = t + 1
                    max_dd_recovery_day = 0
                    in_max_dd_episode = True
                    
        sim_returns[i] = ((current_eq - starting_capital) / starting_capital) * 100.0
        sim_max_dd[i] = max_dd_pct
        
        if max_dd_pct > 0.0:
            if max_dd_recovery_day > 0:
                sim_dd_duration[i] = float(max_dd_recovery_day - max_dd_peak_day)
                sim_recovery_time[i] = float(max_dd_recovery_day - max_dd_trough_day)
                sim_unrecovered[i] = 0.0
            else:
                sim_dd_duration[i] = float(n_obs - max_dd_peak_day)
                sim_recovery_time[i] = float(n_obs - max_dd_trough_day)
                sim_unrecovered[i] = 1.0
        else:
            sim_dd_duration[i] = 0.0
            sim_recovery_time[i] = 0.0
            sim_unrecovered[i] = 0.0
            
        mean_r = sum_ret / n_obs
        var_r = (sum_sq_ret - n_obs * (mean_r ** 2)) / max(1, n_obs - 1)
        std_r = np.sqrt(max(0.0, var_r))
        if std_r > 1e-12:
            sim_sharpe[i] = ((mean_r - rf_daily) / std_r) * np.sqrt(252.0)
        else:
            sim_sharpe[i] = 0.0
            
    return sim_matrix, sim_returns, sim_sharpe, sim_max_dd, sim_dd_duration, sim_recovery_time, sim_unrecovered



def _calc_bootstrap_path_summary(sim_returns, sim_sharpe, sim_max_dd, sim_dd_duration, sim_recovery_time, sim_unrecovered, sim_matrix, starting_capital, dd_thresholds):
    num_sims = len(sim_returns)
    p_sharpe_le_zero = float(np.mean(sim_sharpe <= 0.0) * 100.0)
    
    threshold_probs = {}
    for thresh in dd_thresholds:
        t_val = float(thresh)
        p_exceed = float(np.mean(sim_max_dd >= t_val) * 100.0)
        threshold_probs[f"P(Max DD >= {int(t_val)}%)"] = round(p_exceed, 2)
        
    p5_curve = ((np.percentile(sim_matrix, 5, axis=0) - starting_capital) / starting_capital * 100.0).tolist()
    p25_curve = ((np.percentile(sim_matrix, 25, axis=0) - starting_capital) / starting_capital * 100.0).tolist()
    p50_curve = ((np.percentile(sim_matrix, 50, axis=0) - starting_capital) / starting_capital * 100.0).tolist()
    p75_curve = ((np.percentile(sim_matrix, 75, axis=0) - starting_capital) / starting_capital * 100.0).tolist()
    p95_curve = ((np.percentile(sim_matrix, 95, axis=0) - starting_capital) / starting_capital * 100.0).tolist()

    return {
        "sharpe_median": round(float(np.median(sim_sharpe)), 2),
        "sharpe_p5": round(float(np.percentile(sim_sharpe, 5)), 2),
        "sharpe_p95": round(float(np.percentile(sim_sharpe, 95)), 2),
        "p_sharpe_le_zero": round(p_sharpe_le_zero, 2),
        
        "max_dd_median": round(float(np.median(sim_max_dd)), 2),
        "max_dd_p95": round(float(np.percentile(sim_max_dd, 95)), 2),
        "max_dd_p99": round(float(np.percentile(sim_max_dd, 99)), 2),
        
        "dd_duration_median": round(float(np.median(sim_dd_duration)), 1),
        "dd_duration_p95": round(float(np.percentile(sim_dd_duration, 95)), 1),
        
        "recovery_time_median": round(float(np.median(sim_recovery_time)), 1),
        "recovery_time_p95": round(float(np.percentile(sim_recovery_time, 95)), 1),
        "unrecovered_probability": round(float(np.mean(sim_unrecovered) * 100.0), 2),
        
        "threshold_probabilities": threshold_probs,
        
        "fan_chart": {
            "p5": [round(x, 2) for x in p5_curve],
            "p25": [round(x, 2) for x in p25_curve],
            "p50": [round(x, 2) for x in p50_curve],
            "p75": [round(x, 2) for x in p75_curve],
            "p95": [round(x, 2) for x in p95_curve]
        }
    }

def run_bootstrap_diagnostics(trades_df, starting_capital=100000.0, num_simulations=5000, method="both", block_length="auto", seed=42, dd_thresholds=None, rf_annual=0.04):
    """
    Stationary Block Bootstrap (Politis & Romano, 1994) & IID Bootstrap Diagnostics for CME trading day returns.
    """
    if trades_df.empty or 'Dollar_PnL' not in trades_df.columns or 'ExitTime' not in trades_df.columns:
        return None
        
    df_clean = trades_df.dropna(subset=['ExitTime']).copy()
    if df_clean.empty:
        return None
        
    start_date = df_clean['ExitTime'].min().date()
    end_date = df_clean['ExitTime'].max().date()
    daily_pnl = df_clean.groupby(df_clean['ExitTime'].dt.date)['Dollar_PnL'].sum()
    
    all_bday_dates = _get_trading_days(start_date, end_date)
    df_daily = pd.DataFrame(index=all_bday_dates)
    df_daily['Daily_PnL'] = daily_pnl.reindex(df_daily.index, fill_value=0.0)
    df_daily['Return'] = df_daily['Daily_PnL'] / starting_capital
    
    daily_returns = df_daily['Return'].values.astype(np.float64)
    N_obs = len(daily_returns)
    if N_obs == 0:
        return None
        
    daily_rf = rf_annual / 252.0
    if dd_thresholds is None:
        dd_thresholds = [10.0, 15.0, 20.0, 25.0, 30.0]
        
    if seed is not None:
        np.random.seed(seed)
        
    # 1. Determine optimal block length
    if block_length == "auto" or block_length is None:
        b_opt, b_source = estimate_optimal_block_length_politis_white(daily_returns)
    else:
        b_opt = max(1, int(block_length))
        b_source = "CUSTOM"
        
    p_switch = 1.0 / float(max(1, b_opt))
    
    # 2. Compute Observed Baseline
    obs_stats = calculate_stats(df_clean, starting_capital, rf_annual)
    obs_sharpe = float(obs_stats.get("Sharpe", 0.0))
    obs_max_dd = float(obs_stats.get("Max DD (%)", 0.0))
    obs_dd_dur = float(obs_stats.get("Max Drawdown Duration", 0))
    obs_cagr = float(obs_stats.get("CAGR (%)", 0.0))
    
    obs_equity = starting_capital + df_daily['Daily_PnL'].cumsum()
    obs_peak = obs_equity.cummax()
    obs_underwater = (obs_peak - obs_equity) > 0
    obs_curve_pct = [round(float(x), 2) for x in ((np.insert(obs_equity.values, 0, starting_capital) - starting_capital) / starting_capital * 100.0)]
    
    observed_metrics = {
        "sharpe": round(obs_sharpe, 2),
        "cagr": round(obs_cagr, 2),
        "max_dd": round(obs_max_dd, 2),
        "max_dd_duration": round(obs_dd_dur, 1),
        "curve_pct": obs_curve_pct
    }
    
    result = {
        "metadata": {
            "method": method,
            "seed": seed,
            "simulations": num_simulations,
            "block_length": b_opt,
            "block_length_source": b_source,
            "n_observations": N_obs,
            "trading_calendar": "CME_Equity",
            "return_frequency": "daily"
        },
        "observed": observed_metrics
    }
    
    numba_seed = int(seed) if seed is not None else -1
    
    # 3. IID Bootstrap Resampling
    if method in ["iid", "both"]:
        iid_mat, iid_ret, iid_sh, iid_mdd, iid_dur, iid_rec, iid_unrec = _numba_iid_bootstrap_engine(
            daily_returns, num_simulations, N_obs, starting_capital, daily_rf, numba_seed
        )
        result["iid_bootstrap"] = _calc_bootstrap_path_summary(
            iid_ret, iid_sh, iid_mdd, iid_dur, iid_rec, iid_unrec, iid_mat, starting_capital, dd_thresholds
        )
        
    # 4. Stationary Block Bootstrap Resampling
    if method in ["stationary", "both"]:
        stat_mat, stat_ret, stat_sh, stat_mdd, stat_dur, stat_rec, stat_unrec = _numba_stationary_bootstrap_engine(
            daily_returns, num_simulations, N_obs, p_switch, starting_capital, daily_rf, numba_seed
        )
        result["stationary_bootstrap"] = _calc_bootstrap_path_summary(
            stat_ret, stat_sh, stat_mdd, stat_dur, stat_rec, stat_unrec, stat_mat, starting_capital, dd_thresholds
        )

        
    # 5. Comparative Diagnostics & Path Robustness Heuristic
    if "iid_bootstrap" in result and "stationary_bootstrap" in result:
        stat_95_dd = result["stationary_bootstrap"]["max_dd_p95"]
        iid_95_dd = result["iid_bootstrap"]["max_dd_p95"]
        signed_delta_95 = round(stat_95_dd - iid_95_dd, 2)
        penalty_95 = round(max(0.0, stat_95_dd - iid_95_dd), 2)
        
        stat_99_dd = result["stationary_bootstrap"]["max_dd_p99"]
        iid_99_dd = result["iid_bootstrap"]["max_dd_p99"]
        signed_delta_99 = round(stat_99_dd - iid_99_dd, 2)
        penalty_99 = round(max(0.0, stat_99_dd - iid_99_dd), 2)
        
        p_sharpe_le_0 = result["stationary_bootstrap"]["p_sharpe_le_zero"]
        
        if stat_95_dd <= max(5.0, obs_max_dd * 1.5) and p_sharpe_le_0 <= 5.0:
            heuristic_rating = "ROBUST"
        elif stat_95_dd <= max(10.0, obs_max_dd * 2.2) and p_sharpe_le_0 <= 15.0:
            heuristic_rating = "MODERATE"
        else:
            heuristic_rating = "FRAGILE"
            
        result["comparative"] = {
            "signed_dependence_dd_delta_95": signed_delta_95,
            "dependence_dd_penalty_95": penalty_95,
            "signed_dependence_dd_delta_99": signed_delta_99,
            "dependence_dd_penalty_99": penalty_99,
            "path_robustness_heuristic": heuristic_rating,
            "heuristic_basis": "Empirical threshold classification based on 95th %ile drawdown expansion (<= 1.5x observed) and non-positive Sharpe probability (<= 5%). Informational diagnostic, not a formal hypothesis test."
        }
        
    return result


def _numba_mc_engine(base_sample, num_simulations, sample_size, starting_capital, ruin_threshold, is_bootstrap):
    sim_returns = np.zeros(num_simulations)
    sim_max_dd = np.zeros(num_simulations)
    sim_matrix = np.zeros((num_simulations, sample_size + 1))
    
    for i in prange(num_simulations):
        sim_matrix[i, 0] = starting_capital
        
        if is_bootstrap:
            sampled_pnls = np.random.choice(base_sample, size=sample_size, replace=True)
        else:
            sampled_pnls = np.random.choice(base_sample, size=sample_size, replace=False)
            
        current_eq = starting_capital
        peak_eq = starting_capital
        max_dd = 0.0
        
        for j in range(sample_size):
            current_eq += sampled_pnls[j]
            sim_matrix[i, j + 1] = current_eq
            if current_eq > peak_eq:
                peak_eq = current_eq
            dd = peak_eq - current_eq
            if dd > max_dd:
                max_dd = dd
                
        sim_returns[i] = ((current_eq - starting_capital) / starting_capital) * 100.0
        sim_max_dd[i] = (max_dd / starting_capital) * 100.0
        
    return sim_matrix, sim_returns, sim_max_dd

def run_monte_carlo(trades_df, starting_capital=100000.0, num_simulations=10000, pct_trades=100.0, ruin_threshold=50.0, method="Bootstrap (Resample)", seed=None):
    if trades_df.empty or 'Dollar_PnL' not in trades_df.columns:
        return None
        
    pnls = trades_df['Dollar_PnL'].values
    n_trades = len(pnls)
    if n_trades == 0:
        return None
        
    sample_size = max(1, int(n_trades * (pct_trades / 100.0)))
    
    if seed is not None:
        np.random.seed(seed)
        
    if sample_size < n_trades:
        base_sample = np.random.choice(pnls, size=sample_size, replace=False)
    else:
        base_sample = pnls
    
    is_bootstrap = (method == "Bootstrap (Resample)")
    
    sim_matrix, sim_returns, sim_max_dd = _numba_mc_engine(
        base_sample, num_simulations, sample_size, starting_capital, float(ruin_threshold), is_bootstrap
    )
    
    ruin_count = np.sum(sim_max_dd >= ruin_threshold)
            
    min_eq_per_path = np.min(sim_matrix, axis=1)
    weakest_idx = int(np.argmin(min_eq_per_path))
    
    median_curve = np.median(sim_matrix, axis=0)
    distances = np.linalg.norm(sim_matrix - median_curve, axis=1)
    avg_idx = int(np.argmin(distances))
    
    # 150 background curves for Plotly
    bg_indices = np.random.choice(num_simulations, size=min(num_simulations, 150), replace=False).tolist()
    
    # 95th - 5th Percentile Spread (Dispersion)
    if method == "Permutation (Shuffle)":
        p95_path = np.percentile(sim_matrix, 95, axis=0)
        p5_path = np.percentile(sim_matrix, 5, axis=0)
        max_path_dispersion_pct = float(np.max((p95_path - p5_path) / starting_capital)) * 100.0
        dispersion = max_path_dispersion_pct
        disp_label = 'Max Path Dispersion'
    else:
        p95_ret = float(np.percentile(sim_returns, 95))
        p5_ret = float(np.percentile(sim_returns, 5))
        median_ret = float(np.median(sim_returns))
        
        if abs(median_ret) > 1e-6:
            dispersion = ((p95_ret - p5_ret) / abs(median_ret)) * 100.0
        else:
            dispersion = 0.0
            
        disp_label = 'Dispersion (% of Median)'
    
    return {
        'Median Return': float(np.median(sim_returns)),
        '5th Percentile Return': float(np.percentile(sim_returns, 5)),
        '95th Percentile Return': float(np.percentile(sim_returns, 95)),
        'Dispersion Metric Name': disp_label,
        'Dispersion Value': dispersion,
        'Median Max Drawdown': float(np.median(sim_max_dd)),
        '95th Percentile Max Drawdown': float(np.percentile(sim_max_dd, 95)),
        'Probability of Ruin': float((ruin_count / num_simulations) * 100.0),
        'bg_curves': ((sim_matrix[bg_indices] - starting_capital) / starting_capital * 100.0).tolist(),
        'weakest_curve': ((sim_matrix[weakest_idx] - starting_capital) / starting_capital * 100.0).tolist(),
        'average_curve': ((sim_matrix[avg_idx] - starting_capital) / starting_capital * 100.0).tolist(),
        'original_curve': ((np.cumsum(np.insert(pnls[:sample_size], 0, 0))) / starting_capital * 100.0).tolist(),
        'sample_size': sample_size,
        'sim_returns': sim_returns.tolist(),
        'sim_max_dd': sim_max_dd.tolist()
    }


def get_monthly_heatmap(trades_df, starting_capital=100000.0):
    if trades_df.empty or 'ExitTime' not in trades_df.columns:
        return []
        
    df = trades_df.copy()
    df['YearMonth'] = df['ExitTime'].dt.to_period('M')
    df['Year'] = df['ExitTime'].dt.year
    
    monthly_pnl = df.groupby('YearMonth')['Dollar_PnL'].sum().reset_index()
    monthly_pnl['Year'] = monthly_pnl['YearMonth'].dt.year
    monthly_pnl['Month'] = monthly_pnl['YearMonth'].dt.month
    monthly_pnl['ReturnPct'] = (monthly_pnl['Dollar_PnL'] / starting_capital) * 100
    
    pivot = monthly_pnl.pivot(index='Year', columns='Month', values='ReturnPct')
    pivot.columns = [datetime(2000, m, 1).strftime('%b') for m in pivot.columns]
    
    all_months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    for m in all_months:
        if m not in pivot.columns:
            pivot[m] = None
    pivot = pivot[all_months] # Reorder
    
    # Calculate yearly return and drawdown
    yearly_stats = {}
    for year, group in df.groupby('Year'):
        # Return for the year
        total_pnl = group['Dollar_PnL'].sum()
        ret_pct = (total_pnl / starting_capital) * 100.0
        
        # Max DD for the year
        pnl_in_year = group['Dollar_PnL'].cumsum()
        peak_in_year = np.maximum(0, pnl_in_year.cummax())
        dd = peak_in_year - pnl_in_year
        max_dd = float((dd.max() / starting_capital) * 100.0) if not dd.empty else 0.0
        
        yearly_stats[int(year)] = {'return': ret_pct, 'dd': max_dd}
    
    heatmap_data = []
    for year in pivot.index:
        year_int = int(year)
        row = {"Year": year_int}
        for m in all_months:
            val = pivot.loc[year, m]
            row[m] = float(round(val, 2)) if pd.notna(val) else None
            
        stats = yearly_stats.get(year_int, {'return': 0.0, 'dd': 0.0})
        row['Total Return'] = float(round(stats['return'], 2))
        row['Max Drawdown'] = float(round(stats['dd'], 2))
        
        heatmap_data.append(row)
        
    return heatmap_data

def get_best_worst_moments(trades_df, starting_capital=100000.0):
    if trades_df.empty or 'ExitTime' not in trades_df.columns:
        return {}

    df = trades_df.copy()
    df.set_index('ExitTime', inplace=True)
    df.sort_index(inplace=True)
    
    # Calculate daily, weekly, monthly PnL
    daily_pnl = df['Dollar_PnL'].resample('D').sum()
    weekly_pnl = df['Dollar_PnL'].resample('W').sum()
    monthly_pnl = df['Dollar_PnL'].resample('ME').sum()
    
    # Filter out exactly 0 PnL days (days with no trades)
    daily_pnl = daily_pnl[daily_pnl != 0].dropna()
    weekly_pnl = weekly_pnl[weekly_pnl != 0].dropna()
    monthly_pnl = monthly_pnl[monthly_pnl != 0].dropna()
    
    daily_ret = (daily_pnl / starting_capital) * 100
    weekly_ret = (weekly_pnl / starting_capital) * 100
    monthly_ret = (monthly_pnl / starting_capital) * 100
    
    def get_top_bottom(series, n=10):
        sorted_s = series.sort_values(ascending=False)
        best = sorted_s.head(n)
        worst = sorted_s.tail(n).sort_values(ascending=True)
        return {
            'best': [{'date': k.strftime('%Y-%m-%d'), 'value': round(v, 2)} for k, v in best.items()],
            'worst': [{'date': k.strftime('%Y-%m-%d'), 'value': round(v, 2)} for k, v in worst.items()]
        }

    return {
        'Days': get_top_bottom(daily_ret),
        'Weeks': get_top_bottom(weekly_ret),
        'Months': get_top_bottom(monthly_ret)
    }

def get_drawdown_analysis(trades_df, starting_capital=100000.0):
    if trades_df.empty or 'ExitTime' not in trades_df.columns:
        return [], {}
        
    df = trades_df.sort_values('ExitTime').copy()
    df['cum_pnl'] = df['Dollar_PnL'].cumsum()
    df['peak'] = df['cum_pnl'].cummax()
    df['drawdown'] = df['peak'] - df['cum_pnl']
    df['drawdown_pct'] = (df['drawdown'] / starting_capital) * 100
    
    dd_periods = []
    
    for peak_val, group in df.groupby('peak'):
        max_dd = group['drawdown_pct'].max()
        if max_dd > 0:
            start_date = group['ExitTime'].min()
            end_date = group['ExitTime'].max()
            
            days = (end_date - start_date).days
            dd_periods.append({
                'depth': -max_dd,
                'days': days,
                'start_date': start_date,
                'end_date': end_date
            })
            
    dd_periods.sort(key=lambda x: x['depth']) # More negative is deeper
    top_dds = dd_periods[:10]
    
    result_table = []
    for dd in top_dds:
        result_table.append({
            'depth': round(dd['depth'], 2),
            'days': dd['days'],
            'start_date': dd['start_date'].strftime('%Y-%m-%d'),
            'end_date': dd['end_date'].strftime('%Y-%m-%d')
        })
        
    dd_curve = {
        'x': df['ExitTime'].dt.strftime('%Y-%m-%d %H:%M:%S').tolist(),
        'y': (-df['drawdown_pct']).tolist()
    }
    
    return result_table, dd_curve

def get_trading_time_distribution(trades_df, starting_capital=100000.0):
    if trades_df.empty or 'ExitTime' not in trades_df.columns:
        return {}
        
    df = trades_df.copy()
    if df['ExitTime'].dt.tz is None:
        df['ExitTime'] = df['ExitTime'].dt.tz_localize('UTC').dt.tz_convert('US/Eastern')
    else:
        df['ExitTime'] = df['ExitTime'].dt.tz_convert('US/Eastern')
        
    if 'EntryTime' in df.columns:
        if df['EntryTime'].dt.tz is None:
            df['EntryTime'] = df['EntryTime'].dt.tz_localize('UTC').dt.tz_convert('US/Eastern')
        else:
            df['EntryTime'] = df['EntryTime'].dt.tz_convert('US/Eastern')
            
    df['ExitHalfHour'] = df['ExitTime'].dt.hour + (df['ExitTime'].dt.minute // 30) * 0.5
    if 'EntryTime' in df.columns:
        df['EntryHalfHour'] = df['EntryTime'].dt.hour + (df['EntryTime'].dt.minute // 30) * 0.5
    else:
        df['EntryHalfHour'] = df['ExitHalfHour']
        
    df['Hour'] = df['ExitTime'].dt.hour
    
    def assign_session(h):
        if 3 <= h < 8:
            return 'European'
        elif 8 <= h < 17:
            return 'American'
        else:
            return 'Asian'
            
    df['Session'] = df['Hour'].apply(assign_session)
    
    import numpy as np
    buckets = np.arange(0, 24, 0.5)
    
    volume_by_entry = df.groupby('EntryHalfHour').size().reindex(buckets, fill_value=0).tolist()
    
    pnl_entry_sum = df.groupby('EntryHalfHour')['Dollar_PnL'].sum().reindex(buckets, fill_value=0)
    pnl_entry_pct = (pnl_entry_sum / starting_capital) * 100
    pnl_by_entry = pnl_entry_pct.round(2).tolist()
    
    pnl_exit_sum = df.groupby('ExitHalfHour')['Dollar_PnL'].sum().reindex(buckets, fill_value=0)
    pnl_exit_pct = (pnl_exit_sum / starting_capital) * 100
    pnl_by_exit = pnl_exit_pct.round(2).tolist()
    
    def calc_pf(sub_df):
        gross_profit = sub_df[sub_df['Dollar_PnL'] > 0]['Dollar_PnL'].sum()
        gross_loss = abs(sub_df[sub_df['Dollar_PnL'] < 0]['Dollar_PnL'].sum())
        if gross_loss == 0:
            return round(gross_profit, 2) if gross_profit > 0 else 0.0
        return round(float(gross_profit / gross_loss), 2)
        
    pf_total = df.groupby('EntryHalfHour').apply(calc_pf).reindex(buckets, fill_value=0.0).tolist()
    
    long_df = df[df['direction'] == 'long']
    pf_long = long_df.groupby('EntryHalfHour').apply(calc_pf).reindex(buckets, fill_value=0.0).tolist()
    
    short_df = df[df['direction'] == 'short']
    pf_short = short_df.groupby('EntryHalfHour').apply(calc_pf).reindex(buckets, fill_value=0.0).tolist()
    
    session_data = {}
    total_trades = len(df)
    
    for session in ['Asian', 'European', 'American']:
        session_df = df[df['Session'] == session]
        count = len(session_df)
        session_pnl = float(session_df['Dollar_PnL'].sum()) if count > 0 else 0.0
        ret_pct = (session_pnl / starting_capital) * 100
        
        session_data[session] = {
            'count': count,
            'pct_of_total': float(count / total_trades * 100) if total_trades > 0 else 0.0,
            'return': round(ret_pct, 2)
        }
        
    scatter_data = []
    if 'EntryTime' in df.columns and 'ExitTime' in df.columns:
        for _, row in df.iterrows():
            try:
                if pd.isna(row['EntryTime']) or pd.isna(row['ExitTime']): continue
                entry = row['EntryTime']
                exit_ = row['ExitTime']
                dur = (exit_ - entry).total_seconds()
                scatter_data.append({
                    'pnl': float(row['Dollar_PnL']),
                    'entry_time_str': entry.strftime('1970-01-01 %H:%M:%S'),
                    'exit_time_str': exit_.strftime('1970-01-01 %H:%M:%S'),
                    'duration_sec': int(dur)
                })
            except:
                pass
        
    return {
        'sessions': session_data,
        'volume_by_entry': volume_by_entry,
        'pnl_by_entry': pnl_by_entry,
        'pnl_by_exit': pnl_by_exit,
        'pf_total': pf_total,
        'pf_long': pf_long,
        'pf_short': pf_short,
        'buckets': buckets.tolist(),
        'scatter_data': scatter_data
    }

def get_day_of_week_distribution(trades_df, starting_capital=100000.0):
    if trades_df.empty or 'ExitTime' not in trades_df.columns:
        return {}
        
    df = trades_df.copy()
    df = df.sort_values('ExitTime')
    df['DayOfWeek'] = df['ExitTime'].dt.dayofweek 
    df['Date'] = df['ExitTime'].dt.date
    
    day_names = {0: 'Monday', 1: 'Tuesday', 2: 'Wednesday', 3: 'Thursday', 4: 'Friday'}
    results = {}
    
    for dow in range(5):
        dow_df = df[df['DayOfWeek'] == dow]
        if not dow_df.empty:
            cum_pnl = dow_df['Dollar_PnL'].cumsum()
            ret_pct = (cum_pnl / starting_capital) * 100
            curve_y = ret_pct.round(2).tolist()
            curve_x = list(range(len(curve_y)))
            trading_days = int(dow_df['Date'].nunique())
            total_ret = curve_y[-1] if curve_y else 0.0
            
            gross_profit = dow_df[dow_df['Dollar_PnL'] > 0]['Dollar_PnL'].sum()
            gross_loss = abs(dow_df[dow_df['Dollar_PnL'] < 0]['Dollar_PnL'].sum())
            pf_total = float(gross_profit / gross_loss) if gross_loss != 0 else float(gross_profit)

            long_day = dow_df[dow_df['direction'] == 'long']
            gp_l = long_day[long_day['Dollar_PnL'] > 0]['Dollar_PnL'].sum()
            gl_l = abs(long_day[long_day['Dollar_PnL'] < 0]['Dollar_PnL'].sum())
            pf_long = float(gp_l / gl_l) if gl_l != 0 else float(gp_l)

            short_day = dow_df[dow_df['direction'] == 'short']
            gp_s = short_day[short_day['Dollar_PnL'] > 0]['Dollar_PnL'].sum()
            gl_s = abs(short_day[short_day['Dollar_PnL'] < 0]['Dollar_PnL'].sum())
            pf_short = float(gp_s / gl_s) if gl_s != 0 else float(gp_s)
            
        else:
            curve_x = []
            curve_y = []
            trading_days = 0
            total_ret = 0.0
            pf_total = 0.0
            pf_long = 0.0
            pf_short = 0.0
            
        results[day_names[dow]] = {
            'curve_x': curve_x,
            'curve_y': curve_y,
            'trading_days': trading_days,
            'total_return': total_ret,
            'pf_total': round(pf_total, 2),
            'pf_long': round(pf_long, 2),
            'pf_short': round(pf_short, 2)
        }
        
    return results

def get_monthly_calendar_stats(df, year, month, starting_capital):
    if df.empty or 'Dollar_PnL' not in df.columns:
        return {}
    
    df_month = df[(df['ExitTime'].dt.year == int(year)) & (df['ExitTime'].dt.month == int(month))].copy()
    if df_month.empty:
        return {
            'Return (%)': 0.0,
            'Drawdown (%)': 0.0,
            'PF': 0.0,
            'PF (L)': 0.0,
            'PF (S)': 0.0,
            'daily': {}
        }
    
    net_profit = float(df_month['Dollar_PnL'].sum())
    ret_pct = float(net_profit / starting_capital * 100)
    
    cum_pnl = df_month['Dollar_PnL'].cumsum()
    peak = cum_pnl.cummax()
    drawdown = np.maximum(0, peak - cum_pnl)
    max_dd = float(drawdown.max()) if len(drawdown) > 0 else 0.0
    max_dd_pct = float((max_dd / starting_capital) * 100)
    
    def calc_pf(sub_df):
        gp = sub_df[sub_df['Dollar_PnL'] > 0]['Dollar_PnL'].sum()
        gl = abs(sub_df[sub_df['Dollar_PnL'] < 0]['Dollar_PnL'].sum())
        return float(gp / gl) if gl != 0 else (float(gp) if gp > 0 else 0.0)
        
    pf = calc_pf(df_month)
    pf_l = calc_pf(df_month[df_month['direction'] == 'long'])
    pf_s = calc_pf(df_month[df_month['direction'] == 'short'])
    
    df_month['DateStr'] = df_month['ExitTime'].dt.strftime('%Y-%m-%d')
    daily_grouped = df_month.groupby('DateStr')['Dollar_PnL'].sum().to_dict()
    daily_trades = df_month.groupby('DateStr').size().to_dict()
    daily_longs = df_month[df_month['direction'] == 'long'].groupby('DateStr').size().to_dict()
    daily_shorts = df_month[df_month['direction'] == 'short'].groupby('DateStr').size().to_dict()
    
    daily_stats = {}
    for date_str, pnl in daily_grouped.items():
        daily_stats[date_str] = {
            'pnl': float(pnl),
            'trades': int(daily_trades.get(date_str, 0)),
            'longs': int(daily_longs.get(date_str, 0)),
            'shorts': int(daily_shorts.get(date_str, 0))
        }
        
    return {
        'Return (%)': round(ret_pct, 2),
        'Drawdown (%)': round(max_dd_pct, 2),
        'PF': round(pf, 2),
        'PF (L)': round(pf_l, 2),
        'PF (S)': round(pf_s, 2),
        'daily': daily_stats
    }
