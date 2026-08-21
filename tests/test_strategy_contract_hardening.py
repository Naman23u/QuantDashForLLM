import os
import sys
import unittest
import numpy as np
import pandas as pd
from datetime import datetime, date

# Ensure QuantDash directory is in path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import worker_engine
import quant_metrics
import app as flask_app
from Strategy_Files import template_strategy

class TestStrategyContractHardening(unittest.TestCase):

    def setUp(self):
        # Create standard 5-day synthetic CME RTH DataFrame
        dates = pd.date_range("2023-01-09 09:30:00", periods=5*390, freq="1min", tz="America/New_York")
        rth_dates = [d for d in dates if (d.hour == 9 and d.minute >= 30) or (10 <= d.hour < 16) or (d.hour == 16 and d.minute == 0)]
        n = len(rth_dates)
        np.random.seed(42)
        p = 4000.0 + np.cumsum(np.random.normal(0.05, 0.5, size=n))
        self.sample_df = pd.DataFrame({
            'ts_event': rth_dates,
            'ts_et': rth_dates,
            'open': p,
            'high': p + 1.0,
            'low': p - 1.0,
            'close': p + 0.25,
            'volume': np.full(n, 1000),
            'date': [d.date() for d in rth_dates]
        })

    def test_1_final_bar_entry_and_session_close_prevention(self):
        """
        TEST 1: Canonical Template Final-Bar Entry & Strict Session Close Invariant.
        Verifies that template_strategy.py unconditionally breaks before entry logic
        on the final bar / EOD, and that validate_trade_contract rejects trades past SESSION_CLOSE.
        """
        # Run canonical template on sample data
        res = template_strategy.run_backtest(self.sample_df, {"FAST_PERIOD": 5, "SLOW_PERIOD": 15, "MAX_TRADES_PER_DAY": 5})
        worker_engine.validate_strategy_output_schema(res)
        trades = res.get("trades", [])
        
        contract = worker_engine.get_strategy_contract_metadata(template_strategy)
        self.assertTrue(worker_engine.validate_trade_contract(trades, contract))
        self.assertTrue(worker_engine.validate_trade_contract(trades, contract=None))
        self.assertTrue(worker_engine.validate_trade_contract(trades, contract=template_strategy))
        
        # Test violation: trade exiting at 17:30 ET past 16:00 SESSION_CLOSE
        past_close_trade = [{
            "entry_time": "2023-01-09 10:00:00",
            "exit_time": "2023-01-09 17:30:00", # Exceeds 16:00!
            "direction": "long",
            "entry_price": 4000.0,
            "exit_price": 4005.0,
            "net_pnl": 250.0
        }]
        with self.assertRaises(ValueError) as ctx:
            worker_engine.validate_trade_contract(past_close_trade, contract)
        self.assertIn("Session Close Violation", str(ctx.exception))


    def test_2_trade_validator_inverted_timestamps(self):
        """
        TEST 2: Inverted Timestamps Rejection.
        Asserts that trades with exit_time <= entry_time are rejected with ValueError.
        """
        invalid_trades = [{
            "entry_time": "2023-01-09 10:30:00",
            "exit_time": "2023-01-09 10:15:00", # Inverted!
            "direction": "long",
            "entry_price": 4000.0,
            "exit_price": 4005.0,
            "net_pnl": 250.0
        }]
        with self.assertRaises(ValueError) as ctx:
            worker_engine.validate_trade_contract(invalid_trades)
        self.assertIn("must be on or after", str(ctx.exception))

    def test_3_trade_validator_overlapping_positions_single_model(self):
        """
        TEST 3: Single-Position Overlap Invariant.
        Asserts that concurrent/overlapping trades in a SINGLE position model are rejected.
        """
        overlapping_trades = [
            {
                "entry_time": "2023-01-09 10:00:00",
                "exit_time": "2023-01-09 11:00:00",
                "direction": "long",
                "entry_price": 4000.0,
                "exit_price": 4005.0,
                "net_pnl": 250.0
            },
            {
                "entry_time": "2023-01-09 10:30:00", # Entered before prior exited!
                "exit_time": "2023-01-09 11:30:00",
                "direction": "long",
                "entry_price": 4002.0,
                "exit_price": 4008.0,
                "net_pnl": 300.0
            }
        ]
        contract = {"position_model": "SINGLE", "max_open_positions": 1}
        with self.assertRaises(ValueError) as ctx:
            worker_engine.validate_trade_contract(overlapping_trades, contract)
        self.assertIn("Single-Position Overlap Violation", str(ctx.exception))

    def test_4_trade_validator_multi_position_concurrency_pyramiding_hedging(self):
        """
        TEST 4: Multi-Position Concurrency, Pyramiding, and Hedging Bounds.
        Asserts that MULTI position model permits valid concurrency up to MAX_OPEN_POSITIONS,
        while strictly enforcing ALLOW_PYRAMIDING and ALLOW_HEDGING invariants.
        """
        # A. Valid simultaneous Long + Short when ALLOW_HEDGING is True
        hedged_trades = [
            {"entry_time": "2023-01-09 10:00:00", "exit_time": "2023-01-09 11:00:00", "direction": "long", "entry_price": 4000.0, "exit_price": 4005.0, "net_pnl": 250.0},
            {"entry_time": "2023-01-09 10:15:00", "exit_time": "2023-01-09 11:15:00", "direction": "short", "entry_price": 4002.0, "exit_price": 3998.0, "net_pnl": 200.0},
        ]
        contract_hedged = {"position_model": "MULTI", "max_open_positions": 2, "allow_pyramiding": False, "allow_hedging": True}
        self.assertTrue(worker_engine.validate_trade_contract(hedged_trades, contract_hedged))
        
        # B. Rejection when ALLOW_HEDGING is False
        contract_no_hedge = {"position_model": "MULTI", "max_open_positions": 2, "allow_pyramiding": False, "allow_hedging": False}
        with self.assertRaises(ValueError) as ctx:
            worker_engine.validate_trade_contract(hedged_trades, contract_no_hedge)
        self.assertIn("Hedging Violation", str(ctx.exception))

        # C. Rejection when ALLOW_PYRAMIDING is False on 2 Long positions
        pyramid_trades = [
            {"entry_time": "2023-01-09 10:00:00", "exit_time": "2023-01-09 11:00:00", "direction": "long", "entry_price": 4000.0, "exit_price": 4005.0, "net_pnl": 250.0},
            {"entry_time": "2023-01-09 10:15:00", "exit_time": "2023-01-09 11:15:00", "direction": "long", "entry_price": 4002.0, "exit_price": 4008.0, "net_pnl": 300.0},
        ]
        with self.assertRaises(ValueError) as ctx:
            worker_engine.validate_trade_contract(pyramid_trades, contract_no_hedge)
        self.assertIn("Pyramiding Violation", str(ctx.exception))

        # D. Rejection when active positions exceed MAX_OPEN_POSITIONS
        contract_pyramid = {"position_model": "MULTI", "max_open_positions": 2, "allow_pyramiding": True, "allow_hedging": True}
        self.assertTrue(worker_engine.validate_trade_contract(pyramid_trades, contract_pyramid))
        pyramid_trades.append({"entry_time": "2023-01-09 10:20:00", "exit_time": "2023-01-09 11:20:00", "direction": "long", "entry_price": 4003.0, "exit_price": 4007.0, "net_pnl": 200.0})
        with self.assertRaises(ValueError) as ctx:
            worker_engine.validate_trade_contract(pyramid_trades, contract_pyramid)
        self.assertIn("Multi-Position Concurrency Violation", str(ctx.exception))

    def test_5_trade_validator_nan_inf_numeric_handling(self):
        """
        TEST 5: Non-Finite & NaN Numeric Rejection.
        Asserts that trades with NaN or Inf in price or PnL are rejected.
        """
        nan_trade = [{
            "entry_time": "2023-01-09 10:00:00",
            "exit_time": "2023-01-09 10:30:00",
            "direction": "long",
            "entry_price": 4000.0,
            "exit_price": np.nan, # NaN!
            "net_pnl": 250.0
        }]
        with self.assertRaises(ValueError) as ctx:
            worker_engine.validate_trade_contract(nan_trade)
        self.assertIn("non-finite or NaN", str(ctx.exception))

    def test_6_input_dataframe_immutability(self):
        """
        TEST 6: Input DataFrame Immutability.
        Asserts that strategies mutating the passed DataFrame in-place on any run are caught and failed.
        """
        class RogueMutatingStrategy:
            @staticmethod
            def run_backtest(df, params):
                df["mutated_col"] = 999.99 # Mutates input in-place!
                return {"trades": []}
                
        with self.assertRaises(RuntimeError) as ctx:
            worker_engine.evaluate_strategy_preflight(RogueMutatingStrategy, self.sample_df, {})
        self.assertIn("Pre-Flight Immutability Violation", str(ctx.exception))

    def test_7_dual_canonical_determinism_assertion(self):
        """
        TEST 7: Dual Canonical Determinism Assertion.
        Asserts that strategies producing non-deterministic trades or metrics across
        identical runs are flagged and rejected.
        """
        class NonDeterministicStrategy:
            @staticmethod
            def run_backtest(df, params):
                # Returns random price on each invocation
                rand_p = float(np.random.random() * 100.0)
                return {
                    "trades": [{
                        "entry_time": "2023-01-09 10:00:00",
                        "exit_time": "2023-01-09 10:30:00",
                        "direction": "long",
                        "entry_price": 4000.0,
                        "exit_price": 4000.0 + rand_p,
                        "net_pnl": rand_p * 50.0
                    }]
                }
                
        with self.assertRaises(RuntimeError) as ctx:
            worker_engine.evaluate_strategy_preflight(NonDeterministicStrategy, self.sample_df, {})
        self.assertIn("Pre-Flight Determinism Violation", str(ctx.exception))

    def test_8_fail_loud_warmup_contract(self):
        """
        TEST 8: Fail-Loud Warmup Contract.
        Asserts that broken get_warmup_requirements implementations raise loud RuntimeError
        instead of quietly masking with 1500 bars.
        """
        class BrokenWarmupStrategy:
            @staticmethod
            def get_warmup_requirements(params):
                raise KeyError("NON_EXISTENT_PARAM")
            @staticmethod
            def run_backtest(df, params):
                return {"trades": []}
                
        with self.assertRaises(RuntimeError) as ctx:
            worker_engine.get_strategy_warmup_bars(BrokenWarmupStrategy, {})
        self.assertIn("Strategy Warmup Contract Failure", str(ctx.exception))

    def test_9_wfa_all_trials_failed_abort(self):
        """
        TEST 9: Fail-Loud WFA Cycle Abort with Portable Dedicated Fixtures.
        Asserts that an optimization grid where 100% of candidate trials fail
        aborts the WFA cycle loudly with exact abort message.
        """
        synth_parquet = os.path.join(os.path.dirname(__file__), "fixtures", "synthetic_wfa_3q.parquet")
        failing_strat_path = os.path.join(os.path.dirname(__file__), "fixtures", "always_failing_strategy.py")
        
        client = flask_app.app.test_client()
        payload = {
            "script": failing_strat_path,
            "data": synth_parquet,
            "optimizable_params": [{"name": "PARAM_A", "start": 1, "end": 3, "step": 1}],
            "train_quarters": 2,
            "test_quarters": 1,
            "step_quarters": 1
        }
        resp = client.post('/api/walk_forward', json=payload)
        text = resp.get_data(as_text=True)
        self.assertIn("WFA Cycle 1 Aborted: 0 of 3 parameter combinations produced valid backtest executions", text)

    def test_10_output_schema_contract_validation(self):
        """
        TEST 10: Strategy Output Schema Validation.
        Asserts that non-dict, missing 'trades', or non-list trades fail immediately.
        """
        with self.assertRaises(TypeError):
            worker_engine.validate_strategy_output_schema(None)
            
        with self.assertRaises(TypeError):
            worker_engine.validate_strategy_output_schema([1, 2, 3])
            
        with self.assertRaises(KeyError):
            worker_engine.validate_strategy_output_schema({"metrics": {}, "final_equity": 100000.0})
            
        with self.assertRaises(TypeError):
            worker_engine.validate_strategy_output_schema({"trades": "not_a_list"})
            
        self.assertTrue(worker_engine.validate_strategy_output_schema({"trades": []}))

    def test_11_contract_metadata_strictness_and_contradiction_rejection(self):
        """
        TEST 11: Contract Metadata Strictness & Contradiction Rejection.
        Asserts zero silent fallbacks, strict boolean/integer types, valid IANA timezones,
        and rejection of contradictory contract declarations.
        """
        # A. Non-boolean ALLOW_PYRAMIDING (string 'false')
        class BadBoolStrategy:
            ALLOW_PYRAMIDING = "false"
        with self.assertRaises(TypeError) as ctx:
            worker_engine.get_strategy_contract_metadata(BadBoolStrategy)
        self.assertIn("ALLOW_PYRAMIDING must be a boolean", str(ctx.exception))

        # B. Contradiction: SINGLE with MAX_OPEN_POSITIONS > 1
        class ContradictoryPosStrategy:
            POSITION_MODEL = "SINGLE"
            MAX_OPEN_POSITIONS = 3
        with self.assertRaises(ValueError) as ctx:
            worker_engine.get_strategy_contract_metadata(ContradictoryPosStrategy)
        self.assertIn("POSITION_MODEL is 'SINGLE' but MAX_OPEN_POSITIONS is 3", str(ctx.exception))

        # C. Contradiction: SINGLE with ALLOW_PYRAMIDING = True
        class ContradictoryPyramidStrategy:
            POSITION_MODEL = "SINGLE"
            ALLOW_PYRAMIDING = True
        with self.assertRaises(ValueError) as ctx:
            worker_engine.get_strategy_contract_metadata(ContradictoryPyramidStrategy)
        self.assertIn("POSITION_MODEL is 'SINGLE' but ALLOW_PYRAMIDING is True", str(ctx.exception))

        # D. Invalid Timezone
        class BadTzStrategy:
            SESSION_TIMEZONE = "Mars/Olympus_Mons"
        with self.assertRaises(ValueError) as ctx:
            worker_engine.get_strategy_contract_metadata(BadTzStrategy)
        self.assertIn("SESSION_TIMEZONE 'Mars/Olympus_Mons' is not a valid IANA timezone", str(ctx.exception))

        # E. Invalid Session Close
        class BadCloseStrategy:
            SESSION_CLOSE = "banana"
        with self.assertRaises(ValueError) as ctx:
            worker_engine.get_strategy_contract_metadata(BadCloseStrategy)
        self.assertIn("SESSION_CLOSE must be a valid 'HH:MM'", str(ctx.exception))

    def test_12_api_validate_strategy_endpoint_integration(self):
        """
        TEST 12: Production /api/validate_strategy Endpoint Integration.
        Verifies 200 VALID response for canonical strategy and 400 INVALID response
        for malformed strategy metadata through the HTTP layer.
        """
        client = flask_app.app.test_client()
        canonical_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "Strategy_Files", "template_strategy.py")
        malformed_path = os.path.join(os.path.dirname(__file__), "fixtures", "malformed_contract_strategy.py")

        # 1. Valid canonical strategy -> HTTP 200 + valid: True
        resp_valid = client.post('/api/validate_strategy', json={"script": canonical_path})
        self.assertEqual(resp_valid.status_code, 200)
        data_valid = resp_valid.get_json()
        self.assertTrue(data_valid.get("valid"))
        self.assertEqual(data_valid.get("status"), "VALID")
        self.assertEqual(data_valid.get("contract", {}).get("position_model"), "SINGLE")

        # 2. Malformed strategy -> HTTP 400 + valid: False + detailed error
        resp_invalid = client.post('/api/validate_strategy', json={"script": malformed_path})
        self.assertEqual(resp_invalid.status_code, 400)
        data_invalid = resp_invalid.get_json()
        self.assertFalse(data_invalid.get("valid"))
        self.assertEqual(data_invalid.get("status"), "INVALID")
        self.assertIn("Invalid Strategy Contract", data_invalid.get("error", ""))

    def test_13_trade_contract_default_none_and_module_dispatch(self):
        """
        TEST 13: Contract Dispatch Strictness for Default (None) and Module Objects.
        Asserts that validate_trade_contract handles None, strategy module objects,
        and raw dictionaries uniformly through get_strategy_contract_metadata.
        """
        res = template_strategy.run_backtest(self.sample_df, {"FAST_PERIOD": 5, "SLOW_PERIOD": 15, "MAX_TRADES_PER_DAY": 5})
        trades = res.get("trades", [])
        self.assertGreater(len(trades), 0)
        
        # 1. Default (None) -> canonical single-position intraday flat
        self.assertTrue(worker_engine.validate_trade_contract(trades, contract=None))
        
        # 2. Module object -> extracted metadata
        self.assertTrue(worker_engine.validate_trade_contract(trades, contract=template_strategy))
        
        # 3. Explicit dictionary
        contract_dict = worker_engine.get_strategy_contract_metadata(template_strategy)
        self.assertTrue(worker_engine.validate_trade_contract(trades, contract=contract_dict))

if __name__ == "__main__":
    unittest.main()



