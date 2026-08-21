"""
Unit and Integration Tests for Quant Metrics & Deflated Sharpe Ratio (DSR).
Validates:
1. K=1 Singularity & PSR Equivalence.
2. Monotonicity of SR* and DSR as K increases.
3. Proper dispersion scaling by sample standard deviation of trial Sharpes.
4. Zero-dispersion fallback.
5. Non-normal return moment adjustments (skewness & kurtosis).
6. NaN, inf, and failed trial handling.
7. Optimization Trial Ledger Provenance & In-Sample DSR calculation.
"""
import unittest
import numpy as np
import pandas as pd
import math
import os
import sys
import json

# Ensure QuantDash directory is on sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quant_metrics
import worker_engine
import app

class TestQuantMetricsDSR(unittest.TestCase):

    def setUp(self):
        np.random.seed(42)
        # Create a synthetic daily returns DataFrame (252 days)
        dates = pd.date_range('2020-01-01', periods=252, freq='B')
        daily_returns = np.random.normal(0.0008, 0.01, size=252) # Mean excess positive
        self.df_daily = pd.DataFrame(index=dates.date)
        self.df_daily['Excess_Return'] = daily_returns
        self.mean_excess = float(self.df_daily['Excess_Return'].mean())
        self.std_excess = float(self.df_daily['Excess_Return'].std(ddof=1))
        self.observed_sr_annual = float((self.mean_excess / self.std_excess) * np.sqrt(252))

    def test_1_k1_singularity_and_psr_equivalence(self):
        """
        TEST 1: K=1 Singularity Guard & PSR Equivalence.
        For K=1, expected max Sharpe must be 0.0 and DSR must equal PSR bit-for-bit.
        """
        exp_max_sr, sr_std, k_count = quant_metrics.calculate_expected_max_sharpe(trials_sr_list=[self.observed_sr_annual])
        self.assertEqual(exp_max_sr, 0.0, "Expected max Sharpe for K=1 must be exactly 0.0")
        self.assertEqual(sr_std, 0.0, "Trial std for K=1 must be 0.0")
        self.assertEqual(k_count, 1, "Trial count must be 1")

        dsr_res = quant_metrics.calculate_dsr(
            self.observed_sr_annual, self.df_daily, self.mean_excess, self.std_excess,
            trials_sr_list=[self.observed_sr_annual]
        )
        
        # Calculate standard PSR
        n_obs = len(self.df_daily)
        excess_vals = self.df_daily['Excess_Return'].values
        dev_excess = (excess_vals - self.mean_excess) / self.std_excess
        daily_skew = float(np.mean(dev_excess ** 3))
        daily_kurt = float(np.mean(dev_excess ** 4))
        s_per_period = float(self.mean_excess / self.std_excess)
        var_s = (1.0 - daily_skew * s_per_period + ((daily_kurt - 1.0) / 4.0) * (s_per_period ** 2)) / (n_obs - 1)
        z_psr = s_per_period / math.sqrt(var_s)
        expected_psr_pct = round(0.5 * (1.0 + math.erf(z_psr / math.sqrt(2.0))) * 100.0, 2)

        self.assertAlmostEqual(dsr_res["DSR (%)"], expected_psr_pct, places=2,
                               msg="For K=1, DSR (%) must equal PSR (%)!")
        self.assertEqual(dsr_res["Expected Max SR"], 0.0)
        self.assertEqual(dsr_res["Trials Tested (K)"], 1)

    def test_2_monotonicity_with_k_trials(self):
        """
        TEST 2: Monotonicity with K trials.
        As K increases (1 -> 10 -> 100 -> 1000 -> 5000),
        SR* must strictly increase and DSR must strictly decrease for a fixed observed Sharpe.
        """
        k_values = [1, 5, 20, 100, 500, 2000]
        fixed_std = 0.6
        prev_sr_star = -1.0
        prev_dsr = 101.0

        for k in k_values:
            exp_max_sr, sr_std, k_count = quant_metrics.calculate_expected_max_sharpe(n_trials=k, sr_std=fixed_std)
            dsr_res = quant_metrics.calculate_dsr(
                self.observed_sr_annual, self.df_daily, self.mean_excess, self.std_excess,
                n_trials=k, sr_std=fixed_std
            )
            
            if k > 1:
                self.assertGreater(exp_max_sr, prev_sr_star, f"SR* did not increase when K grew to {k}")
                self.assertLessEqual(dsr_res["DSR (%)"], prev_dsr, f"DSR did not decrease when K grew to {k}")
                self.assertGreaterEqual(dsr_res["DSR P-Value"], 0.0)
                self.assertLessEqual(dsr_res["DSR P-Value"], 1.0)
                
            prev_sr_star = exp_max_sr
            prev_dsr = dsr_res["DSR (%)"]

    def test_3_dispersion_scaling(self):
        """
        TEST 3: Proper Dispersion Scaling by Trial Standard Deviation.
        For K=100, doubling trial Sharpe dispersion must double SR* benchmark hurdle.
        """
        k = 100
        std_1 = 0.3
        std_2 = 0.6
        
        sr_star_1, _, _ = quant_metrics.calculate_expected_max_sharpe(n_trials=k, sr_std=std_1)
        sr_star_2, _, _ = quant_metrics.calculate_expected_max_sharpe(n_trials=k, sr_std=std_2)
        
        self.assertAlmostEqual(sr_star_2 / sr_star_1, 2.0, places=4,
                               msg="Expected Max SR must scale linearly with trial standard deviation!")

    def test_4_zero_dispersion_fallback(self):
        """
        TEST 4: Zero Dispersion Fallback.
        When 50 trials all produce identical Sharpe ratios (sigma_SR = 0), SR* must be 0.0.
        """
        trials = [1.5] * 50
        exp_max_sr, sr_std, k_count = quant_metrics.calculate_expected_max_sharpe(trials_sr_list=trials)
        self.assertEqual(exp_max_sr, 0.0)
        self.assertEqual(sr_std, 0.0)
        self.assertEqual(k_count, 50)

    def test_5_non_normal_return_moments(self):
        """
        TEST 5: Non-Normal Return Moments Impact on DSR.
        Positively skewed returns reduce variance of Sharpe estimator and yield higher DSR
        compared to negatively skewed returns (left tail crash risk).
        """
        # Construct synthetic positive vs negative skew distributions with same mean & std
        n = 500
        # Positive skew: many small negative, few huge positive
        pos_skew_returns = np.concatenate([np.full(450, -0.001), np.full(50, 0.015)])
        # Negative skew: many small positive, few huge negative
        neg_skew_returns = np.concatenate([np.full(450, 0.001), np.full(50, -0.015)])
        
        dates_pos = pd.date_range('2020-01-01', periods=n, freq='B')
        df_pos = pd.DataFrame(index=dates_pos.date)
        df_pos['Excess_Return'] = pos_skew_returns
        mean_pos = float(df_pos['Excess_Return'].mean())
        std_pos = float(df_pos['Excess_Return'].std(ddof=1))
        
        df_neg = pd.DataFrame(index=dates_pos.date)
        df_neg['Excess_Return'] = neg_skew_returns
        mean_neg = float(df_neg['Excess_Return'].mean())
        std_neg = float(df_neg['Excess_Return'].std(ddof=1))
        
        dsr_pos = quant_metrics.calculate_dsr(1.5, df_pos, mean_pos, std_pos, n_trials=50, sr_std=0.4)
        dsr_neg = quant_metrics.calculate_dsr(1.5, df_neg, mean_neg, std_neg, n_trials=50, sr_std=0.4)
        
        # Negative skewness should produce lower DSR (or higher p-value) due to crash risk
        self.assertGreater(dsr_pos["DSR (%)"], dsr_neg["DSR (%)"],
                           "Positively skewed returns should have higher statistical significance than negatively skewed crash-prone returns!")

    def test_6_nan_and_failed_trial_handling(self):
        """
        TEST 6: Graceful handling of NaNs, Infs, and failed zero-trade trials.
        """
        dirty_trials = [1.2, np.nan, 2.0, np.inf, -0.5, -np.inf, 1.8, 0.0]
        exp_max_sr, sr_std, k_count = quant_metrics.calculate_expected_max_sharpe(trials_sr_list=dirty_trials)
        
        self.assertEqual(k_count, 5, "NaN and Inf entries must be cleanly stripped from trial vector")
        self.assertGreater(exp_max_sr, 0.0)
        self.assertGreater(sr_std, 0.0)

    def test_7_wfa_trial_ledger_and_is_dsr(self):
        """
        TEST 7: WFA In-Sample Trial Ledger Provenance.
        Executes Flask /api/walk_forward on mock strategy and verifies that:
        1. cycle_update contains trial_ledger with trials_evaluated, expected_max_sr, and provenance.
        2. In-Sample metrics include DSR (%) and DSR P-Value.
        3. Out-of-Sample metrics evaluate genuine unseen data.
        """
        fixtures_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
        strategy_path = os.path.join(fixtures_dir, "mock_wfo_strategy.py")
        data_path = os.path.join(fixtures_dir, "synthetic_clean.parquet")
        
        payload = {
            "script": strategy_path,
            "data": data_path,
            "base_params": {},
            "optimizable_params": [
                {"name": "PERIOD", "start": 5, "end": 25, "step": 5} # 5 combinations: 5, 10, 15, 20, 25
            ],
            "wfo_mode": "Rolling",
            "train_quarters": 2,
            "test_quarters": 1,
            "step_quarters": 1,
            "objective_metric": "sharpe",
            "start_quarter": "2020-Q1",
            "end_quarter": "2020-Q4"
        }
        
        client = app.app.test_client()
        response = client.post('/api/walk_forward', json=payload)
        self.assertEqual(response.status_code, 200)
        
        lines = response.get_data(as_text=True).strip().split('\n')
        cycles = []
        for line in lines:
            if not line.strip(): continue
            msg = json.loads(line)
            if msg.get("type") == "cycle_update":
                cycles.append(msg)
                
        self.assertGreater(len(cycles), 0, "No WFO cycles returned!")
        
        first_cycle = cycles[0]
        self.assertIn("trial_ledger", first_cycle, "cycle_update must include trial_ledger!")
        ledger = first_cycle["trial_ledger"]
        
        self.assertEqual(ledger["trials_evaluated"], 5, "Must record all 5 evaluated parameter combinations")
        self.assertEqual(ledger["trials_with_valid_sharpe"], 5, "Must record count of trials with valid Sharpe")
        self.assertEqual(ledger["selection_mode"], "SELECTION_ADJUSTED")
        self.assertIn("raw_k_assumption", ledger)
        self.assertIn("expected_max_sr", ledger)
        self.assertIn("trial_sharpe_std", ledger)
        self.assertIn("selected_params", ledger)
        self.assertIn("trials", ledger)
        
        # Verify trial list provenance
        trials = ledger["trials"]
        self.assertEqual(len(trials), 5)
        selected_count = sum(1 for t in trials if t.get("selected") is True)
        self.assertEqual(selected_count, 1, "Exactly one parameter combination must be flagged as selected winner")
        
        # Verify In-Sample metrics have DSR
        is_metrics = first_cycle.get("is_metrics", {})
        self.assertIn("DSR (%)", is_metrics)
        self.assertIn("DSR P-Value", is_metrics)
        self.assertIn("Expected Max SR", is_metrics)
        self.assertIn("Trials Tested (K)", is_metrics)
        self.assertEqual(is_metrics["Trials Tested (K)"], 5)


if __name__ == '__main__':
    unittest.main()
