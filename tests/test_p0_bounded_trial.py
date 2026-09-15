import argparse
import os
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import main
from src.analyzer import AnalysisResult, GeminiAnalyzer, P0ModelBoundaryError
from src.core.pipeline import P0BoundedTrialError, StockAnalysisPipeline
from src.enums import ReportType
from src.services.factor_decision_summary import (
    apply_canonical_decision_to_result,
    build_stock_factor_decision_summary,
)


def _trend(**overrides):
    data = {
        "signal_score": 82,
        "trend_status": SimpleNamespace(value="多头排列"),
        "buy_signal": SimpleNamespace(value="买入"),
        "ma_alignment": "MA5 > MA10 > MA20",
        "trend_strength": 78,
        "current_price": 10.5,
        "support_levels": [10.0],
        "resistance_levels": [11.3],
        "volume_status": SimpleNamespace(value="缩量回调"),
        "volume_ratio_5d": 0.62,
        "volume_trend": "缩量回调",
        "macd_signal": "MACD多头结构",
        "rsi_signal": "RSI中性偏强",
        "risk_factors": [],
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def _canonical_result(code):
    result = AnalysisResult(
        code=code,
        name=code,
        sentiment_score=80,
        trend_prediction="看多",
        operation_advice="买入",
        decision_type="buy",
        action="buy",
        action_label="买入",
        analysis_summary="LLM 建议买入",
        buy_reason="LLM 看多",
        dashboard={
            "core_conclusion": {"one_sentence": "买入"},
            "phase_decision": {"immediate_action": "买入"},
            "strategy_synthesis": {"final_signal": "buy"},
            "battle_plan": {
                "sniper_points": {"ideal_buy": "10.00"},
                "action_checklist": ["买入"],
            },
        },
    )
    summary = build_stock_factor_decision_summary(_trend(), include_canonical=True)
    return apply_canonical_decision_to_result(result, summary)


def _bounded_analyzer():
    analyzer = GeminiAnalyzer.__new__(GeminiAnalyzer)
    analyzer._config_override = SimpleNamespace(
        generation_backend="litellm",
        generation_fallback_backend="",
        litellm_model="openai/test-model",
        llm_model_list=[],
        openai_api_keys=[],
        openai_base_url="",
    )
    analyzer._p0_bounded_trial = True
    analyzer._p0_request_lock = threading.Lock()
    analyzer._p0_model_request_count = 0
    analyzer._router = None
    analyzer._litellm_available = True
    return analyzer


def test_one_and_two_ordinary_a_share_codes_are_accepted():
    assert main.validate_p0_stock_codes("600519") == ["600519"]
    assert main.validate_p0_stock_codes("SH600519,000001.SZ") == ["600519", "000001"]


@pytest.mark.parametrize(
    "raw_codes",
    [
        "600519,000001,000002",
        "510300",
        "000300",
        "AAPL",
        "600519.SZ",
        "SH600519.SZ",
    ],
)
def test_unbounded_etf_index_non_cn_and_exchange_conflicts_are_rejected(raw_codes):
    with pytest.raises(main.P0BoundedTrialError):
        main.validate_p0_stock_codes(raw_codes)


def test_process_local_bounds_do_not_read_or_overwrite_stock_list(monkeypatch):
    monkeypatch.setenv("STOCK_LIST", "300750,601318")
    config = SimpleNamespace(
        single_stock_notify=True,
        merge_email_notification=True,
        report_type="full",
        report_language="en",
        report_integrity_retry=3,
        agent_mode=True,
        agent_skills=["all"],
        analysis_delay=9,
    )
    args = argparse.Namespace(workers=8, no_market_review=False)

    main._apply_p0_runtime_config(config, args)

    assert os.environ["STOCK_LIST"] == "300750,601318"
    assert config.single_stock_notify is False
    assert config.merge_email_notification is False
    assert config.report_type == "simple"
    assert config.report_language == "zh"
    assert config.report_integrity_retry == 0
    assert config.agent_mode is False
    assert config.agent_skills == []
    assert args.workers == 1
    assert args.no_market_review is True


def test_p0_pipeline_does_not_initialize_search_social_or_agent_router():
    config = SimpleNamespace(
        max_workers=4,
        save_context_snapshot=False,
        daily_market_context_enabled=False,
        enable_realtime_quote=False,
        realtime_source_priority=[],
        enable_chip_distribution=False,
    )
    analyzer = MagicMock()

    with patch("src.core.pipeline.get_db", return_value=MagicMock()), \
         patch("src.core.pipeline.DataFetcherManager", return_value=MagicMock()), \
         patch("src.core.pipeline.StockTrendAnalyzer", return_value=MagicMock()), \
         patch("src.core.pipeline.GeminiAnalyzer", return_value=analyzer) as analyzer_cls, \
         patch("src.core.pipeline.NotificationService", return_value=MagicMock()), \
         patch("src.core.pipeline.MarketStructureService", return_value=MagicMock()), \
         patch("src.core.pipeline.MarketHotspotService") as hotspot_cls, \
         patch("src.core.pipeline.SearchService") as search_cls, \
         patch("src.core.pipeline.SocialSentimentService") as social_cls:
        pipeline = StockAnalysisPipeline(
            config=config,
            p0_bounded_trial=True,
            p0_stock_codes=["600519"],
        )

    assert pipeline.max_workers == 1
    assert pipeline.search_service is None
    assert pipeline.social_sentiment_service is None
    search_cls.assert_not_called()
    social_cls.assert_not_called()
    hotspot_cls.assert_not_called()
    analyzer_cls.assert_called_once_with(
        config=config,
        skills=None,
        p0_bounded_trial=True,
    )


def test_p0_analyzer_initialization_does_not_construct_router_or_fallbacks():
    analyzer = _bounded_analyzer()
    analyzer._litellm_available = False

    with patch("src.analyzer.Router") as router:
        analyzer._init_litellm()

    router.assert_not_called()
    assert analyzer._router is None
    assert analyzer._litellm_available is True


def test_p0_direct_litellm_dispatch_has_two_request_budget_and_no_recovery():
    analyzer = _bounded_analyzer()
    response = {"choices": [{"message": {"content": "{}"}}]}

    with patch("src.analyzer.litellm.completion", return_value=response) as completion, \
         patch.object(analyzer, "_normalize_usage", return_value={}), \
         patch("src.analyzer.apply_litellm_generation_params") as param_recovery, \
         patch("src.analyzer.call_litellm_with_param_recovery") as transport_recovery:
        for _ in range(2):
            text, model, _usage = analyzer._call_litellm_p0_bounded(
                "分析",
                {"temperature": 0.2, "max_output_tokens": 99_999},
                response_validator=lambda value: None,
            )
            assert text == "{}"
            assert model == "openai/test-model"

        with pytest.raises(P0ModelBoundaryError, match="budget exceeded"):
            analyzer._call_litellm_p0_bounded("第三次", {"temperature": 0.2})

    assert completion.call_count == 2
    for call in completion.call_args_list:
        kwargs = call.kwargs
        assert kwargs["model"] == "openai/test-model"
        assert kwargs["stream"] is False
        assert kwargs["num_retries"] == 0
        assert kwargs["max_tokens"] == 4096
    param_recovery.assert_not_called()
    transport_recovery.assert_not_called()


def test_p0_transport_failure_is_not_retried_or_fallback_dispatched():
    analyzer = _bounded_analyzer()
    analyzer._router = MagicMock()

    with patch("src.analyzer.litellm.completion", side_effect=RuntimeError("offline")) as completion:
        with pytest.raises(P0ModelBoundaryError, match="direct LiteLLM request failed"):
            analyzer._call_litellm_p0_bounded("分析", {"temperature": 0.2})

    completion.assert_called_once()
    analyzer._router.completion.assert_not_called()
    assert analyzer.p0_model_request_count == 1


def test_p0_failed_explanation_promotes_only_proven_deterministic_result():
    pipeline = StockAnalysisPipeline.__new__(StockAnalysisPipeline)
    pipeline.p0_bounded_trial = True
    result = _canonical_result("600519")
    result.success = False
    result.error_message = "provider unavailable"

    assert pipeline._promote_p0_deterministic_result_after_explanation_failure(
        result,
        code="600519",
    ) is True
    assert result.success is True
    assert result.error_message is None
    factor = result.dashboard["factor_decision"]
    assert factor["canonical_decision"]["evidence_state"] == "PROVEN"
    assert factor["explanation_status"] == {
        "state": "UNAVAILABLE",
        "mode": "DETERMINISTIC_DEGRADED",
        "reason": "LLM_EXPLANATION_UNAVAILABLE",
    }
    assert factor["investor_brief"]["explanation_status"] == factor["explanation_status"]
    assert result.action == factor["canonical_decision"]["public_action"]


def test_p0_failed_explanation_does_not_promote_unknown_deterministic_evidence():
    pipeline = StockAnalysisPipeline.__new__(StockAnalysisPipeline)
    pipeline.p0_bounded_trial = True
    result = _canonical_result("600519")
    unknown = build_stock_factor_decision_summary(None, include_canonical=True)
    result.dashboard["factor_decision"] = unknown
    apply_canonical_decision_to_result(result, unknown)
    result.success = False
    result.error_message = "provider unavailable"

    assert pipeline._promote_p0_deterministic_result_after_explanation_failure(
        result,
        code="600519",
    ) is False
    assert result.success is False
    assert result.error_message == "provider unavailable"
    assert result.dashboard["factor_decision"]["canonical_decision"]["evidence_state"] == "UNKNOWN"
    assert "explanation_status" not in result.dashboard["factor_decision"]


def test_p0_message_byte_ceiling_fails_before_dispatch():
    analyzer = _bounded_analyzer()

    with patch("src.analyzer.litellm.completion") as completion:
        with pytest.raises(P0ModelBoundaryError, match="UTF-8 bytes"):
            analyzer._call_litellm_p0_bounded("中" * 70_000, {"temperature": 0.2})

    completion.assert_not_called()
    assert analyzer.p0_model_request_count == 0


def test_two_stock_trial_saves_full_audit_and_emails_compact_from_same_results():
    pipeline = StockAnalysisPipeline.__new__(StockAnalysisPipeline)
    pipeline.notifier = MagicMock()
    pipeline.notifier.generate_aggregate_report.return_value = "full canonical audit"
    pipeline.notifier.generate_brief_report.return_value = "compact investor notification"
    pipeline.notifier.save_report_to_file.return_value = "reports/report.md"
    pipeline.notifier.send_to_email.return_value = True
    results = [_canonical_result("600519"), _canonical_result("000001")]

    report = pipeline._finalize_p0_bounded_run(
        results,
        ["600519", "000001"],
        ReportType.SIMPLE,
    )

    assert report == "full canonical audit"
    pipeline.notifier.generate_aggregate_report.assert_called_once_with(
        results,
        ReportType.SIMPLE,
    )
    pipeline.notifier.generate_brief_report.assert_called_once_with(results)
    assert pipeline.notifier.generate_aggregate_report.call_args.args[0] is results
    assert pipeline.notifier.generate_brief_report.call_args.args[0] is results
    pipeline.notifier.save_report_to_file.assert_called_once_with("full canonical audit")
    pipeline.notifier.send_to_email.assert_called_once_with("compact investor notification")
    pipeline.notifier.send.assert_not_called()


def test_bounded_audit_only_mode_saves_full_report_without_notification_projection():
    pipeline = StockAnalysisPipeline.__new__(StockAnalysisPipeline)
    pipeline.notifier = MagicMock()
    pipeline.notifier.generate_aggregate_report.return_value = "full canonical audit"
    pipeline.notifier.save_report_to_file.return_value = "reports/report.md"
    results = [_canonical_result("600519")]

    report = pipeline._finalize_p0_bounded_run(
        results,
        ["600519"],
        ReportType.SIMPLE,
        send_notification=False,
    )

    assert report == "full canonical audit"
    pipeline.notifier.generate_aggregate_report.assert_called_once_with(
        results,
        ReportType.SIMPLE,
    )
    pipeline.notifier.save_report_to_file.assert_called_once_with("full canonical audit")
    pipeline.notifier.generate_brief_report.assert_not_called()
    pipeline.notifier.send_to_email.assert_not_called()
    pipeline.notifier.send.assert_not_called()


@pytest.mark.parametrize(
    "failure",
    ["target", "conflict", "empty_audit", "empty_notification"],
)
def test_target_conflict_or_empty_projection_produces_zero_notification(failure):
    pipeline = StockAnalysisPipeline.__new__(StockAnalysisPipeline)
    pipeline.notifier = MagicMock()
    pipeline.notifier.generate_aggregate_report.return_value = (
        "" if failure == "empty_audit" else "full audit"
    )
    pipeline.notifier.generate_brief_report.return_value = (
        "" if failure == "empty_notification" else "compact notification"
    )
    pipeline.notifier.send_to_email.return_value = True
    results = [_canonical_result("600519")]
    targets = ["600519"]
    if failure == "target":
        targets.append("000001")
    elif failure == "conflict":
        results[0].action = "buy"

    with pytest.raises((P0BoundedTrialError, ValueError)):
        pipeline._finalize_p0_bounded_run(results, targets, ReportType.SIMPLE)

    pipeline.notifier.send_to_email.assert_not_called()

# P0_REPORT_LANGUAGE_CONSUMER_REGRESSION_PY_R001
import inspect as _pyrec_p0_inspect

from src.report_language import (
    localize_strategy_synthesis_summary as _pyrec_p0_localize_summary,
    normalize_strategy_synthesis_payload as _pyrec_p0_normalize_payload,
)


def _pyrec_p0_call_language_aware(fn, value):
    signature = _pyrec_p0_inspect.signature(fn)
    parameters = list(signature.parameters.values())
    required_positionals = [
        p
        for p in parameters
        if p.kind
        in (
            _pyrec_p0_inspect.Parameter.POSITIONAL_ONLY,
            _pyrec_p0_inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
        and p.default is _pyrec_p0_inspect.Parameter.empty
    ]

    if len(required_positionals) <= 1:
        for p in parameters:
            if (
                p.kind == _pyrec_p0_inspect.Parameter.KEYWORD_ONLY
                and p.name in {"language", "lang"}
            ):
                return fn(value, **{p.name: "zh"})
        return fn(value)

    second = required_positionals[1]
    if second.name in {"language", "lang"}:
        return fn(value, "zh")

    raise AssertionError(
        f"Unexpected consumer signature for {fn.__name__}: {signature}"
    )


def test_p0_report_language_consumer_blocks_legacy_hold_leak():
    payload = {
        "authority": "stock_trend_quality_pullback_v1",
        "canonical_public_action": "avoid",
        "final_signal": "hold",
    }

    normalized = _pyrec_p0_normalize_payload(payload)
    assert payload["final_signal"] == "hold"
    assert normalized["final_signal"] == "avoid"

    summary = str(
        _pyrec_p0_call_language_aware(
            _pyrec_p0_localize_summary,
            payload,
        )
    )
    assert "\u56de\u907f" in summary
    assert "\u6301\u6709" not in summary
