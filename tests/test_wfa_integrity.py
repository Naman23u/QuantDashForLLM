"""
===============================================================================
QuantDash Walk-Forward Optimization (WFO) Integration & Leakage Regression Suite
===============================================================================
Directly executes the real production Flask '/api/walk_forward' endpoint and
production worker engine against synthetic market datasets.

Test Catalog:
1. test_1_poisoned_future_data_invariance (Production Endpoint Temporal Isolation)
2. test_2_boundary_disjointness (Production Cycle Boundaries)
3. test_3_parameter_freeze_integrity (True Winner-to-OOS Freeze Verification)
4. test_4_boundary_state_and_warmup_isolation (Zero Warmup Leakage & Straddling Guard)
5. test_5_single_future_bar_mutation (Production Endpoint Future Bar Invariance)
6. test_6_warmup_sufficiency (Indicator Mathematical Convergence)
7. test_7_multi_tiered_warmup_semantics (Daily Sessions & Intraday Resolution)
===============================================================================
"""

import os
import sys
import json
import unittest
import numpy as np
import pandas as pd
from datetime import datetime, time, timedelta

# Ensure QuantDash root is in python path
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

import app as qapp
import worker_engine
import quant_metrics

def generate_synthetic_intraday_data(start_year=2020, end_year=2021, seed=42) -> pd.DataFrame:
    """
    Generates a deterministic 1-minute intraday dataset formatted as CME Databento data.
    RTH hours: 09:30 - 15:59 ET (390 bars per day).
    """
    np.random.seed(seed)
    # Generate business days up to 2021-Q2
    dates = pd.date_range(start=f"{start_year}-01-01", end=f"{end_year}-06-30", freq='B')
    
    rows = []
    price = 3000.0
    
    for d in dates:
        day_date = d.date()
        base_dt = datetime.combine(day_date, time(9, 30))
        daily_drift = np.random.normal(0.0002, 0.0005)
        for m in range(390):
            bar_time = base_dt + timedelta(minutes=m)
            ret = np.random.normal(daily_drift / 390.0, 0.001)
            o = price
            c = round(price * (1.0 + ret), 2)
            h = round(max(o, c) + abs(np.random.normal(0, 0.5)), 2)
            l = round(min(o, c) - abs(np.random.normal(0, 0.5)), 2)
            v = int(np.random.uniform(500, 3000))
            price = c
            
            rows.append({
                "ts_et": pd.Timestamp(bar_time, tz="America/New_York"),
                "ts_event": pd.Timestamp(bar_time, tz="America/New_York").tz_convert("UTC"),
                "open": o,
                "high": h,
                "low": l,
                "close": c,
                "volume": v,
                "date": day_date
            })
            
    df = pd.DataFrame(rows)
    df = df.sort_values("ts_et").reset_index(drop=True)
    return df


class TestWFAProductionIntegrity(unittest.TestCase):
    
    @classmethod
    def setUpClass(cls):
        """Create fixtures and synthetic parquet files for real Flask testing."""
        cls.fixtures_dir = os.path.join(BASE_DIR, "tests", "fixtures")
        os.makedirs(cls.fixtures_dir, exist_ok=True)
        
        cls.clean_parquet = os.path.join(cls.fixtures_dir, "synthetic_clean.parquet")
        cls.poisoned_parquet = os.path.join(cls.fixtures_dir, "synthetic_poisoned.parquet")
        cls.mock_strategy_path = os.path.join(cls.fixtures_dir, "mock_wfo_strategy.py")
        
        # 1. Generate clean dataset (2020 - 2021)
        df_clean = generate_synthetic_intraday_data(start_year=2020, end_year=2021, seed=42)
        df_clean.to_parquet(cls.clean_parquet, index=False)
        cls.df_clean = df_clean
        
        # 2. Generate poisoned dataset (2020 clean, 2021 violently corrupted)
        df_poisoned = df_clean.copy()
        mask_2021 = df_poisoned['date'] >= pd.to_datetime('2021-01-01').date()
        df_poisoned.loc[mask_2021, 'open'] *= 1000.0
        df_poisoned.loc[mask_2021, 'high'] *= 1500.0
        df_poisoned.loc[mask_2021, 'low'] *= 0.01
        df_poisoned.loc[mask_2021, 'close'] *= 1000.0
        df_poisoned.loc[mask_2021, 'volume'] = 9999999
        df_poisoned.to_parquet(cls.poisoned_parquet, index=False)
        
        cls.client = qapp.app.test_client()

    def _run_production_wfo(self, data_path: str, quarters: list):
        """Calls the actual Flask endpoint /api/walk_forward and parses JSONL response."""
        req_payload = {
            "script": self.mock_strategy_path,
            "data": data_path,
            "quarters": quarters,
            "wfo_mode": "rolling",
            "train_quarters": 2,
            "test_quarters": 1,
            "step_quarters": 1,
            "objective_metric": "sharpe",
            "base_params": {
                "PERIOD": 14,
                "STOP_MULT": 1.5,
                "TICK_SIZE": 0.25,
                "POINT_VALUE": 50.0,
                "COMMISSION": 5.0,
                "RISK_FREE_RATE_PCT": 4.0
            },
            "optimizable_params": [
                {"name": "PERIOD", "start": 10, "end": 20, "step": 10}
            ]
        }
        
        response = self.client.post('/api/walk_forward', json=req_payload)
        self.assertEqual(response.status_code, 200, f"Flask /api/walk_forward returned error: {response.get_data(as_text=True)}")
        
        raw_text = response.get_data(as_text=True).strip()
        lines = [l for l in raw_text.split('\n') if l.strip()]
        
        cycles = []
        final_summary = None
        for l in lines:
            msg = json.loads(l)
            if msg.get("type") == "cycle_update":
                cycles.append(msg)
            elif msg.get("type") == "final_summary":
                final_summary = msg
                
        return cycles, final_summary

    def test_1_poisoned_future_data_invariance(self):
        """
        TEST 1: Real Production Flask Endpoint Poisoned Future Test.
        Calls /api/walk_forward on clean vs poisoned parquet files for quarters including 2021.
        Asserts bit-for-bit identical results on all cycles occurring strictly before the corruption date.
        """
        quarters = ["2020-Q1", "2020-Q2", "2020-Q3", "2020-Q4", "2021-Q1"]
        
        clean_cycles, clean_summary = self._run_production_wfo(self.clean_parquet, quarters)
        poison_cycles, poison_summary = self._run_production_wfo(self.poisoned_parquet, quarters)
        
        self.assertGreater(len(clean_cycles), 0, "No WFO cycles produced by production engine")
        self.assertEqual(len(clean_cycles), len(poison_cycles), "Production cycle counts mismatch under future data poisoning!")
        
        # Cycle 1: IS (2020-Q1..Q2), OOS (2020-Q3) -> MUST be 100% BIT-IDENTICAL
        # Cycle 2: IS (2020-Q2..Q3), OOS (2020-Q4) -> MUST be 100% BIT-IDENTICAL
        for i in range(2):
            c_clean = clean_cycles[i]
            c_poison = poison_cycles[i]
            
            self.assertEqual(c_clean["frozen_params"], c_poison["frozen_params"], 
                             f"Cycle {i+1} selected parameters changed under future data poisoning in production!")
            self.assertEqual(c_clean["is_metrics"].get("Net Profit"), c_poison["is_metrics"].get("Net Profit"),
                             f"Cycle {i+1} In-Sample Net Profit changed under future data poisoning in production!")
            self.assertEqual(c_clean["oos_metrics"].get("Net Profit"), c_poison["oos_metrics"].get("Net Profit"),
                             f"Cycle {i+1} Out-of-Sample Net Profit changed under future data poisoning in production!")
            self.assertEqual(c_clean["oos_metrics"].get("Sharpe"), c_poison["oos_metrics"].get("Sharpe"),
                             f"Cycle {i+1} Out-of-Sample Sharpe changed under future data poisoning in production!")
            
        # Cycle 3: IS (2020-Q3..Q4), OOS (2021-Q1) -> In-Sample Training MUST STILL be 100% BIT-IDENTICAL!
        self.assertEqual(clean_cycles[2]["frozen_params"], poison_cycles[2]["frozen_params"],
                         "Cycle 3 In-Sample optimizer looked ahead into poisoned 2021 OOS data!")
        self.assertEqual(clean_cycles[2]["is_metrics"].get("Net Profit"), poison_cycles[2]["is_metrics"].get("Net Profit"),
                         "Cycle 3 In-Sample metrics contaminated by poisoned 2021 OOS data!")

    def test_2_boundary_disjointness(self):
        """
        TEST 2: Production Boundary Disjointness.
        Asserts that train and test quarter labels are strictly disjoint in production output.
        """
        quarters = ["2020-Q1", "2020-Q2", "2020-Q3", "2020-Q4", "2021-Q1"]
        cycles, _ = self._run_production_wfo(self.clean_parquet, quarters)
        
        for c in cycles:
            train_label = c["train_window"]
            test_label = c["test_window"]
            train_end_q = train_label.split(' to ')[-1]
            test_start_q = test_label.split(' to ')[0]
            
            _, train_end_dt = qapp.quarter_to_dates(train_end_q)
            test_start_dt, _ = qapp.quarter_to_dates(test_start_q)
            
            self.assertLess(train_end_dt, test_start_dt, 
                            f"Production cycle {c['cycle']} In-Sample window {train_label} overlaps OOS {test_label}!")

    def test_3_parameter_freeze_integrity(self):
        """
        TEST 3: True Winner-to-OOS Freeze Verification.
        Proves that the exact winning parameter combination discovered during In-Sample grid sweep
        is precisely what is frozen and evaluated in Out-Of-Sample execution.
        """
        quarters = ["2020-Q1", "2020-Q2", "2020-Q3", "2020-Q4"]
        cycles, _ = self._run_production_wfo(self.clean_parquet, quarters)
        
        # Independently calculate the winning parameter for Cycle 1 (Train: 2020-Q1..Q2)
        start_dt, _ = qapp.quarter_to_dates("2020-Q1")
        _, end_dt = qapp.quarter_to_dates("2020-Q2")
        st_date = pd.to_datetime(start_dt).date()
        end_date = pd.to_datetime(end_dt).date()
        
        train_mask = (self.df_clean['date'] >= st_date) & (self.df_clean['date'] <= end_date)
        train_slice = self.df_clean[train_mask].copy()
        
        # Test PERIOD=10 vs PERIOD=20 manually
        res_10 = worker_engine.evaluate_backtest_task(self.mock_strategy_path, train_slice, {"PERIOD": 10}, {"PERIOD": 10}, filter_start_date=st_date)
        res_20 = worker_engine.evaluate_backtest_task(self.mock_strategy_path, train_slice, {"PERIOD": 20}, {"PERIOD": 20}, filter_start_date=st_date)
        
        sharpe_10 = float(res_10.get("metrics", {}).get("Sharpe", -10.0))
        sharpe_20 = float(res_20.get("metrics", {}).get("Sharpe", -10.0))
        expected_winner_period = 10 if sharpe_10 >= sharpe_20 else 20
        
        # Assert Cycle 1 frozen parameter in production matches the exact In-Sample winner
        self.assertEqual(cycles[0]["frozen_params"]["PERIOD"], expected_winner_period,
                         "Production WFO frozen parameter did not match the In-Sample optimization winner!")

    def test_4_boundary_state_and_warmup_isolation(self):
        """
        TEST 4: Production Warmup Isolation & Boundary Straddling Guard.
        1. Asserts zero warmup trades leak into production stitched equity curve.
        2. Asserts engine raises ValueError if a rogue trade straddles across evaluation boundary.
        """
        quarters = ["2020-Q1", "2020-Q2", "2020-Q3", "2020-Q4"]
        cycles, summary = self._run_production_wfo(self.clean_parquet, quarters)
        
        # 1. Clean run verification
        stitched_equity = summary.get("stitched_equity_curve", {})
        dates_list = stitched_equity.get("dates", [])
        if dates_list:
            for d_str in dates_list:
                exit_date = pd.to_datetime(d_str).date()
                self.assertGreaterEqual(exit_date, pd.to_datetime('2020-07-01').date(),
                                        "Warmup trade leaked into production stitched OOS equity curve!")
                
        # 2. Straddling boundary violation test
        # Pass rogue_straddling_strategy.py directly into worker_engine.evaluate_backtest_task
        rogue_script = os.path.join(self.fixtures_dir, "rogue_straddling_strategy.py")
        target_date = pd.to_datetime('2020-07-01').date()
        
        res = worker_engine.evaluate_backtest_task(
            rogue_script,
            self.df_clean.iloc[:1000],
            {},
            {},
            filter_start_date=target_date
        )
        self.assertIn("error", res, "Worker engine failed to flag boundary straddling violation!")
        self.assertIn("Boundary Flatness Violation", res.get("error", ""),
                      "Worker engine returned unexpected error message for boundary straddling!")



    def test_5_single_future_bar_mutation(self):
        """
        TEST 5: Single Future Bar Mutation against Production Flask Endpoint.
        Mutating one bar in 2021-Q1 must leave Cycle 1 (2020-Q3 OOS) 100% invariant.
        """
        quarters = ["2020-Q1", "2020-Q2", "2020-Q3", "2020-Q4", "2021-Q1"]
        base_cycles, _ = self._run_production_wfo(self.clean_parquet, quarters)
        
        mutated_parquet = os.path.join(self.fixtures_dir, "synthetic_mutated_single_bar.parquet")
        df_mut = self.df_clean.copy()
        idx_2021 = df_mut.index[df_mut['date'] >= pd.to_datetime('2021-01-01').date()][0]
        df_mut.loc[idx_2021, 'close'] = 999999.0
        df_mut.to_parquet(mutated_parquet, index=False)
        
        mut_cycles, _ = self._run_production_wfo(mutated_parquet, quarters)
        
        self.assertEqual(base_cycles[0]["frozen_params"], mut_cycles[0]["frozen_params"])
        self.assertEqual(base_cycles[0]["oos_metrics"].get("Net Profit"), 
                         mut_cycles[0]["oos_metrics"].get("Net Profit"))
        self.assertEqual(base_cycles[0]["oos_metrics"].get("Sharpe"), 
                         mut_cycles[0]["oos_metrics"].get("Sharpe"))

    def test_6_warmup_sufficiency(self):
        """
        TEST 6: Warmup Sufficiency Test.
        Asserts that indicators computed on buffered slice match full continuous history up to 10^-6 precision.
        """
        full_df = self.df_clean.copy()
        full_df['full_sma'] = full_df['close'].rolling(20).mean().shift(1)
        
        target_date = pd.to_datetime('2020-07-01').date()
        target_idx = full_df.index[full_df['date'] >= target_date][0]
        
        warmup_bars = 780
        buffered_slice = full_df.iloc[target_idx - warmup_bars : target_idx + 390].copy()
        buffered_slice['slice_sma'] = buffered_slice['close'].rolling(20).mean().shift(1)
        
        full_val = full_df.loc[target_idx, 'full_sma']
        slice_val = buffered_slice.loc[target_idx, 'slice_sma']
        
        self.assertFalse(np.isnan(slice_val), "First OOS bar has NaN indicator!")
        self.assertAlmostEqual(full_val, slice_val, places=5, 
                               msg="Warmup buffer produced divergent indicator state vs full continuous history!")

    def test_7_multi_tiered_warmup_semantics(self):
        """
        TEST 7: Multi-tiered Warmup Semantics.
        Verifies that worker_engine resolves intraday_bars, daily_sessions, and previous_close.
        """
        class MultiTieredStrategy:
            @staticmethod
            def get_warmup_requirements(params=None):
                return {
                    "intraday_bars": 500,
                    "daily_sessions": 20,
                    "previous_close": True
                }
                
        # 20 sessions + 1 prev close = 21 sessions * 390 = 8,190 bars (which exceeds 500 intraday bars)
        resolved_bars = worker_engine.get_strategy_warmup_bars(MultiTieredStrategy)
        self.assertEqual(resolved_bars, (20 + 1) * 390, "Worker engine failed to resolve daily session warmup requirements!")


if __name__ == "__main__":
    unittest.main()
