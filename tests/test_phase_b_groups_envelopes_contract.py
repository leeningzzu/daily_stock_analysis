from __future__ import annotations

import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class TestPhaseBGroupsEnvelopesContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.main = (ROOT / "main.py").read_text(encoding="utf-8")
        cls.screening = (ROOT / "src/services/screening_service.py").read_text(encoding="utf-8")
        cls.screening_pipeline = (
            ROOT / "src/services/screening/pipeline.py"
        ).read_text(encoding="utf-8")
        cls.snapshot = (
            ROOT / "src/services/screening/snapshot.py"
        ).read_text(encoding="utf-8")
        cls.pit = (ROOT / "src/services/pit_identity.py").read_text(encoding="utf-8")
        cls.pipeline = (ROOT / "src/core/pipeline.py").read_text(encoding="utf-8")
        cls.notification = (ROOT / "src/notification.py").read_text(encoding="utf-8")
        cls.workflow = (
            ROOT / ".github/workflows/00-daily-analysis.yml"
        ).read_text(encoding="utf-8")
        cls.etf_strategy = (
            ROOT
            / "src/services/screening/strategies/etf_candidate_prefilter.yaml"
        ).read_text(encoding="utf-8")

    def test_main_parser_exposes_independent_stock_and_etf_caps(self) -> None:
        tree = ast.parse(self.main)
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_argument"
        ]
        rendered = {ast.unparse(call) for call in calls}
        stock = next(item for item in rendered if "'--auto-screen-max-results'" in item)
        etf = next(item for item in rendered if "'--auto-screen-etf-max-results'" in item)
        self.assertIn("tuple(range(1, 11))", stock)
        self.assertIn("tuple(range(0, 11))", etf)
        self.assertIn("'--watchlist-conditional'", "\n".join(rendered))

    def test_auto_screen_groups_have_independent_focus_and_remaining_buckets(self) -> None:
        self.assertIn(
            '"AUTO_ETF" if asset_type == "etf" else "AUTO_STOCK"',
            self.screening,
        )
        self.assertIn('f"{group_prefix}_FOCUS"', self.screening)
        self.assertIn('f"{group_prefix}_REMAINING"', self.screening)
        self.assertIn('"group_rank"', self.screening)
        self.assertIn('"asset_type"', self.screening)
        self.assertIn("1 <= int(max_results) <= 10", self.screening)
        self.assertIn("0 <= int(etf_max_results) <= 10", self.screening)
        self.assertIn('"stock_codes": stock_codes', self.screening)
        self.assertIn('"etf_codes": etf_codes', self.screening)

    def test_etf_screening_reuses_existing_pipeline_without_stock_valuation_filters(self) -> None:
        self.assertIn('asset_type: str = "stock"', self.screening_pipeline)
        self.assertIn("fetch_cn_etf_snapshot", self.snapshot)
        self.assertIn("AkshareFetcher().get_etf_realtime_snapshot()", self.snapshot)
        self.assertIn("factor_weights:", self.etf_strategy)
        self.assertIn("liquidity:", self.etf_strategy)
        self.assertIn("momentum:", self.etf_strategy)
        self.assertIn("activity:", self.etf_strategy)
        self.assertIn("stability:", self.etf_strategy)
        self.assertIn("name: etf_candidate_prefilter", self.etf_strategy)
        self.assertNotIn("name: etf_relative_strength_rotation", self.etf_strategy)
        self.assertIn("不声称已具备同类ETF相对强弱", self.etf_strategy)
        self.assertNotIn("pe_min:", self.etf_strategy)
        self.assertNotIn("pe_max:", self.etf_strategy)
        self.assertNotIn("pb_min:", self.etf_strategy)
        self.assertNotIn("pb_max:", self.etf_strategy)
        self.assertNotIn("total_mv_min:", self.etf_strategy)
        self.assertNotIn("circ_mv_min:", self.etf_strategy)

    def test_selection_identity_binds_auto_and_watchlist_envelopes(self) -> None:
        self.assertIn('"ASSET_RESEARCH_BRIEF_AUTO"', self.pit)
        self.assertIn('"delivery_envelope"', self.pit)
        self.assertIn("ASSET_RESEARCH_BRIEF_WATCHLIST", self.main)

    def test_watchlist_material_change_uses_history_and_skips_same_run_auto(self) -> None:
        self.assertIn("get_analysis_history(", self.pipeline)
        self.assertIn('== "AUTO_SCREEN"', self.pipeline)
        self.assertIn("CANONICAL_FACTS_CHANGED", self.pipeline)
        self.assertIn("FIRST_COMPARABLE_ANALYSIS", self.pipeline)
        self.assertIn("HISTORY_COMPARISON_UNAVAILABLE", self.pipeline)
        self.assertIn("sha256_payload", self.pipeline)
        self.assertNotIn("notification_dedup_ttl_seconds", self.pipeline)
        self.assertNotIn("evaluate_notification_noise", self.pipeline)

    def test_saved_watchlist_audit_does_not_overwrite_auto_report(self) -> None:
        self.assertIn(
            "report_{datetime.now().strftime('%Y%m%d')}_watchlist.md",
            self.pipeline,
        )
        self.assertIn("完整审计已保存", self.pipeline)

    def test_notification_projects_top3_remaining_and_watchlist_without_new_facts(self) -> None:
        for label in (
            "ETF重点 Top 3",
            "股票重点 Top 3",
            "ETF其余候选 4–10",
            "股票其余候选 4–10",
            "我的自选研究",
        ):
            self.assertIn(label, self.notification)
        self.assertIn("_research_conclusion", self.notification)
        self.assertIn('dashboard.get("factor_decision")', self.notification)

    def test_schedule_runs_fixed_auto_then_conditional_watchlist(self) -> None:
        self.assertIn("cron: '0 11 * * 1-5'", self.workflow)
        self.assertIn(
            "python main.py --auto-screen --auto-screen-max-results 10 "
            "--auto-screen-etf-max-results 10 --no-market-review $FORCE_RUN_ARG",
            self.workflow,
        )
        self.assertIn(
            "python main.py --watchlist-conditional --no-market-review $FORCE_RUN_ARG",
            self.workflow,
        )
        self.assertIn('WATCHLIST_HAS_CODES="false"', self.workflow)
        self.assertIn('WATCHLIST_HAS_CODES="true"', self.workflow)

    def test_p0_bounded_live_remains_stock_only_one_candidate(self) -> None:
        self.assertIn(
            "bounded live requires max_results=1 and etf_max_results=0",
            self.main,
        )
        self.assertIn(
            "bounded live requires stock max=1 and ETF max=0",
            self.workflow,
        )
        self.assertIn("AUTO_SCREEN_BOUNDED_MODEL", self.workflow)


if __name__ == "__main__":
    unittest.main()
