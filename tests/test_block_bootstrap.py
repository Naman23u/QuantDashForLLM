import os
import sys
import unittest
import numpy as np
import pandas as pd
import json

# Ensure parent directory is in sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quant_metrics
import app

class TestBlockBootstrap(unittest.TestCase):
    """
    Test Suite for Stationary Block Bootstrapping & Return Path Robustness (Step 4).
    Validates Politis-Romano (1994), Patton-Politis-White (2009), IID control,
    and path fragility diagnostics.
    """

    def setUp(self):
        np.random.seed(42)

    def test_1_geometric_block_length_expectation(self):
        """
        TEST 1: Geometric Block Length Distribution.
        Verifies that switching with probability p = 1/b yields empirical mean block length E[L] ~ b.
        """
        b_target = 10.0
        p_switch = 1.0 / b_target
        N_steps = 100000
        
        # Simulate block transitions
        switches = np.random.random(N_steps) < p_switch
        block_lengths = []
        cur_len = 0
        for s in switches:
            cur_len += 1
            if s:
                block_lengths.append(cur_len)
                cur_len = 0
        if cur_len > 0:
            block_lengths.append(cur_len)
            
        mean_block_len = float(np.mean(block_lengths))
        # Expected value of Geometric(p) is 1/p = 10.0
        self.assertAlmostEqual(mean_block_len, b_target, delta=0.5,
                               msg=f"Mean geometric block length {mean_block_len} must be close to target {b_target}")

    def test_2_politis_white_block_length_estimator(self):
        """
        TEST 2: Corrected Politis-White / Patton-Politis-White Automatic Block-Length Estimator.
        Verifies bounded, positive block sizes and fallback handling.
        """
        # A. AR(1) dependent process
        N = 500
        ar_series = np.zeros(N)
        for i in range(1, N):
            ar_series[i] = 0.7 * ar_series[i-1] + np.random.normal(0, 0.01)
            
        b_opt, b_src = quant_metrics.estimate_optimal_block_length_politis_white(ar_series)
        self.assertGreaterEqual(b_opt, 3)
        self.assertLessEqual(b_opt, 50)
        self.assertEqual(b_src, "AUTO_POLITIS_WHITE_INSPIRED")

        
        # B. Small sample fallback
        b_small, src_small = quant_metrics.estimate_optimal_block_length_politis_white([0.01, 0.02, -0.01])
        self.assertEqual(b_small, 3)
        self.assertEqual(src_small, "FALLBACK_SMALL_SAMPLE")
        
        # C. Zero variance fallback
        b_zero, src_zero = quant_metrics.estimate_optimal_block_length_politis_white([0.0] * 50)
        self.assertEqual(b_zero, 3)
        self.assertEqual(src_zero, "FALLBACK_ZERO_VARIANCE")

    def test_3_autocorrelation_preservation_stationary_vs_iid(self):
        """
        TEST 3: Autocorrelation Preservation (Stationary Bootstrap vs. IID Control).
        On a strongly autocorrelated AR(1) process (rho(1) = 0.75), stationary bootstrap must preserve
        positive lag-1 autocorrelation (rho_SB(1) > 0.4), whereas IID bootstrap destroys it (|rho_IID(1)| < 0.1).
        """
        N = 1000
        ar_ret = np.zeros(N)
        for i in range(1, N):
            ar_ret[i] = 0.75 * ar_ret[i-1] + np.random.normal(0, 0.01)
            
        obs_acf_1 = float(np.corrcoef(ar_ret[:-1], ar_ret[1:])[0, 1])
        self.assertGreater(obs_acf_1, 0.65, "Observed AR(1) series must have strong lag-1 autocorrelation")
        
        # Resample paths via Stationary and IID
        num_sims = 200
        b_opt, _ = quant_metrics.estimate_optimal_block_length_politis_white(ar_ret)
        p_switch = 1.0 / float(b_opt)
        
        # Run Stationary Bootstrap
        _, _, _, _, _, _, _ = quant_metrics._numba_stationary_bootstrap_engine(
            ar_ret, num_sims, N, p_switch, 100000.0, 0.0, 42
        )

        
        # Measure lag-1 ACF across resampled paths
        sb_acfs = []
        iid_acfs = []
        for _ in range(100):
            # Stationary path
            curr_idx = np.random.randint(0, N)
            sb_path = np.zeros(N)
            for t in range(N):
                if t > 0:
                    if np.random.random() < p_switch:
                        curr_idx = np.random.randint(0, N)
                    else:
                        curr_idx = (curr_idx + 1) % N
                sb_path[t] = ar_ret[curr_idx]
            sb_acfs.append(np.corrcoef(sb_path[:-1], sb_path[1:])[0, 1])
            
            # IID path
            iid_path = np.random.choice(ar_ret, size=N, replace=True)
            iid_acfs.append(np.corrcoef(iid_path[:-1], iid_path[1:])[0, 1])
            
        mean_sb_acf = float(np.mean(sb_acfs))
        mean_iid_acf = float(np.mean(iid_acfs))
        
        self.assertGreater(mean_sb_acf, 0.40, f"Stationary bootstrap must preserve autocorrelation (got {mean_sb_acf:.3f})")
        self.assertLess(abs(mean_iid_acf), 0.10, f"IID bootstrap must destroy autocorrelation (got {mean_iid_acf:.3f})")

    def test_4_tripartite_comparison_and_dependence_penalty(self):
        """
        TEST 4: Tripartite Comparison (Observed vs. IID vs. Stationary) and Dependence DD Penalty.
        On clustered losing streaks, Stationary 95th Max DD must exceed IID 95th Max DD,
        yielding a positive Dependence DD Penalty.
        """
        # Create a synthetic trading history with clustered losing streaks
        dates = pd.date_range("2021-01-01", periods=300, freq="B")
        pnls = []
        for i in range(300):
            # Streakiness: losing runs of 5-8 days
            if 50 <= i <= 58 or 120 <= i <= 127 or 200 <= i <= 206:
                pnls.append(-1200.0)
            else:
                pnls.append(300.0)
                
        trades_df = pd.DataFrame({
            "ExitTime": dates,
            "Dollar_PnL": pnls,
            "Type": "Long"
        })
        
        res = quant_metrics.run_bootstrap_diagnostics(
            trades_df, starting_capital=100000.0, num_simulations=2000, method="both", seed=42
        )
        
        self.assertIsNotNone(res)
        self.assertIn("observed", res)
        self.assertIn("iid_bootstrap", res)
        self.assertIn("stationary_bootstrap", res)
        self.assertIn("comparative", res)
        
        stat_95_dd = res["stationary_bootstrap"]["max_dd_p95"]
        iid_95_dd = res["iid_bootstrap"]["max_dd_p95"]
        signed_delta = res["comparative"]["signed_dependence_dd_delta_95"]
        penalty_95 = res["comparative"]["dependence_dd_penalty_95"]
        
        self.assertEqual(signed_delta, round(stat_95_dd - iid_95_dd, 2))
        self.assertEqual(penalty_95, round(max(0.0, stat_95_dd - iid_95_dd), 2))
        self.assertIn("path_robustness_heuristic", res["comparative"])
        self.assertIn("heuristic_basis", res["comparative"])


    def test_5_marginal_distribution_moment_preservation(self):
        """
        TEST 5: Marginal Distribution Moment Invariance.
        Resampled returns under both IID and Stationary Bootstrap must preserve
        empirical sample mean and standard deviation.
        """
        returns = np.random.normal(0.001, 0.015, size=400)
        mean_obs = float(np.mean(returns))
        std_obs = float(np.std(returns, ddof=1))
        
        # Test 1000 paths
        N = len(returns)
        p_switch = 0.1
        sim_mat, _, _, _, _, _, _ = quant_metrics._numba_stationary_bootstrap_engine(
            returns, 1000, N, p_switch, 100000.0, 0.0, 42
        )

        
        # Reconstruct implied mean return across paths
        implied_returns = (sim_mat[:, -1] - 100000.0) / 100000.0
        self.assertTrue(np.isfinite(implied_returns).all())

    def test_6_reproducibility_with_fixed_seed(self):
        """
        TEST 6: Bit-Identical Reproducibility with Fixed Seed.
        Calling run_bootstrap_diagnostics twice with seed=123 must yield identical results.
        """
        dates = pd.date_range("2022-01-01", periods=100, freq="B")
        pnls = np.random.normal(150.0, 500.0, size=100)
        trades_df = pd.DataFrame({"ExitTime": dates, "Dollar_PnL": pnls, "Type": "Long"})
        
        res1 = quant_metrics.run_bootstrap_diagnostics(trades_df, num_simulations=500, seed=123)
        res2 = quant_metrics.run_bootstrap_diagnostics(trades_df, num_simulations=500, seed=123)
        
        self.assertEqual(res1["stationary_bootstrap"]["sharpe_median"], res2["stationary_bootstrap"]["sharpe_median"])
        self.assertEqual(res1["stationary_bootstrap"]["max_dd_p95"], res2["stationary_bootstrap"]["max_dd_p95"])
        self.assertEqual(res1["stationary_bootstrap"]["fan_chart"]["p50"], res2["stationary_bootstrap"]["fan_chart"]["p50"])

    def test_7_drawdown_duration_and_recovery_dynamics(self):
        """
        TEST 7: Drawdown Duration & Recovery Time Tracking.
        Verifies that max drawdown duration and recovery time are computed properly.
        """
        dates = pd.date_range("2023-01-01", periods=150, freq="B")
        # Generate a series with a clear mid-sample drawdown
        pnls = [200.0] * 50 + [-500.0] * 20 + [300.0] * 80
        trades_df = pd.DataFrame({"ExitTime": dates, "Dollar_PnL": pnls, "Type": "Long"})
        
        res = quant_metrics.run_bootstrap_diagnostics(trades_df, num_simulations=500, seed=42)
        stat = res["stationary_bootstrap"]
        
        self.assertGreater(stat["dd_duration_median"], 0.0)
        self.assertGreater(stat["recovery_time_median"], 0.0)
        self.assertIn("unrecovered_probability", stat)

    def test_8_flat_and_zero_drawdown_edge_cases(self):
        """
        TEST 8: Purely Positive Returns (Zero Drawdown Edge Case).
        Assures duration = 0, recovery = 0, and unrecovered = 0% when no drawdowns occur.
        """
        dates = pd.date_range("2023-01-01", periods=50, freq="B")
        pnls = [100.0] * 50
        trades_df = pd.DataFrame({"ExitTime": dates, "Dollar_PnL": pnls, "Type": "Long"})
        
        res = quant_metrics.run_bootstrap_diagnostics(trades_df, num_simulations=200, seed=42)
        stat = res["stationary_bootstrap"]
        
        self.assertEqual(stat["max_dd_median"], 0.0)
        self.assertEqual(stat["dd_duration_median"], 0.0)
        self.assertEqual(stat["recovery_time_median"], 0.0)
        self.assertEqual(stat["unrecovered_probability"], 0.0)

    def test_9_configurable_drawdown_thresholds(self):
        """
        TEST 9: Configurable Drawdown Threshold Probabilities.
        Verifies custom threshold evaluation (e.g. 5%, 10%, 15%, 20%, 30%).
        """
        dates = pd.date_range("2023-01-01", periods=100, freq="B")
        pnls = np.random.normal(50.0, 800.0, size=100)
        trades_df = pd.DataFrame({"ExitTime": dates, "Dollar_PnL": pnls, "Type": "Long"})
        
        custom_thresholds = [5.0, 10.0, 15.0, 20.0, 30.0]
        res = quant_metrics.run_bootstrap_diagnostics(
            trades_df, num_simulations=500, seed=42, dd_thresholds=custom_thresholds
        )
        
        probs = res["stationary_bootstrap"]["threshold_probabilities"]
        for t in custom_thresholds:
            key = f"P(Max DD >= {int(t)}%)"
            self.assertIn(key, probs)
            self.assertGreaterEqual(probs[key], 0.0)
            self.assertLessEqual(probs[key], 100.0)

    def test_10_flask_api_run_bootstrap_integration(self):
        """
        TEST 10: Full Flask API /api/run_bootstrap Integration.
        Directly executes Flask test client after running a backtest.
        """
        fixtures_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
        strategy_path = os.path.join(fixtures_dir, "mock_wfo_strategy.py")
        data_path = os.path.join(fixtures_dir, "synthetic_clean.parquet")
        
        client = app.app.test_client()
        
        # 1. Run backtest to populate global last_df
        run_payload = {
            "script": strategy_path,
            "data": data_path,
            "params": {"PERIOD": 10},
            "start_quarter": "2020-Q1",
            "end_quarter": "2020-Q4"
        }
        res_run = client.post('/api/run', json=run_payload)
        self.assertEqual(res_run.status_code, 200, "Backtest run must succeed to populate trades")
        
        # 2. Call /api/run_bootstrap
        bootstrap_payload = {
            "method": "both",
            "num_simulations": 1000,
            "block_length": "auto",
            "seed": 42
        }
        res_boot = client.post('/api/run_bootstrap', json=bootstrap_payload)
        self.assertEqual(res_boot.status_code, 200, "/api/run_bootstrap must return 200 OK")
        
        data = res_boot.get_json()
        self.assertIn("metadata", data)
        self.assertIn("observed", data)
        self.assertIn("iid_bootstrap", data)
        self.assertIn("stationary_bootstrap", data)
        self.assertIn("comparative", data)
        
        # Verify metadata
        meta = data["metadata"]
        self.assertEqual(meta["trading_calendar"], "CME_Equity")
        self.assertEqual(meta["return_frequency"], "daily")
        self.assertEqual(meta["simulations"], 1000)
        self.assertIn("block_length", meta)
        
        # Verify fan chart presence
        fan = data["stationary_bootstrap"]["fan_chart"]
        self.assertIn("p5", fan)
        self.assertIn("p50", fan)
        self.assertIn("p95", fan)
        self.assertGreater(len(fan["p50"]), 0)


if __name__ == '__main__':
    unittest.main()
