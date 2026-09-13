from pathlib import Path
import os
import sys
import tempfile
import types
import unittest


WORKFLOW_PATH = (
    Path(__file__).resolve().parents[1]
    / ".github"
    / "workflows"
    / "00-daily-analysis.yml"
)


class TestDailyAnalysisStrictSchedule(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = WORKFLOW_PATH.read_text(encoding="utf-8")

    def _gate_source(self):
        gate_id = self.text.index("id: strict_cn_schedule_gate")
        marker = "          python - <<'PY'\n"
        start = self.text.index(marker, gate_id) + len(marker)
        end = self.text.index("\n          PY", start)
        body = self.text[start:end]

        lines = []
        for line in body.splitlines():
            if line.startswith("          "):
                lines.append(line[10:])
            else:
                lines.append(line)

        return "\n".join(lines)

    def _run_gate(self, trading_day=None, raises=False):
        src_pkg = types.ModuleType("src")
        src_pkg.__path__ = []

        core_pkg = types.ModuleType("src.core")
        core_pkg.__path__ = []

        calendar_pkg = types.ModuleType("src.core.trading_calendar")

        def build_market_phase_context(**kwargs):
            self.assertEqual(kwargs["market"], "cn")
            self.assertEqual(kwargs["analysis_phase"], "auto")
            self.assertEqual(kwargs["trigger_source"], "github_schedule")

            if raises:
                raise RuntimeError("synthetic calendar failure")

            if trading_day is True:
                phase = "postmarket"
            elif trading_day is False:
                phase = "non_trading"
            else:
                phase = "unknown"

            return types.SimpleNamespace(
                is_trading_day=trading_day,
                phase=types.SimpleNamespace(value=phase),
                warnings=[],
            )

        calendar_pkg.build_market_phase_context = build_market_phase_context

        src_pkg.core = core_pkg
        core_pkg.trading_calendar = calendar_pkg

        names = (
            "src",
            "src.core",
            "src.core.trading_calendar",
        )

        old_modules = {name: sys.modules.get(name) for name in names}

        sys.modules["src"] = src_pkg
        sys.modules["src.core"] = core_pkg
        sys.modules["src.core.trading_calendar"] = calendar_pkg

        old_output = os.environ.get("GITHUB_OUTPUT")

        try:
            with tempfile.TemporaryDirectory() as tmp:
                output_path = Path(tmp) / "github_output.txt"
                os.environ["GITHUB_OUTPUT"] = str(output_path)

                exec(
                    compile(self._gate_source(), "<workflow_gate>", "exec"),
                    {"__name__": "__main__"},
                )

                result = {}

                for line in output_path.read_text(
                    encoding="utf-8"
                ).splitlines():
                    key, value = line.split("=", 1)
                    result[key] = value

                return result

        finally:
            if old_output is None:
                os.environ.pop("GITHUB_OUTPUT", None)
            else:
                os.environ["GITHUB_OUTPUT"] = old_output

            for name, module in old_modules.items():
                if module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = module

    def test_exactly_one_schedule_at_1900_shanghai(self):
        import re

        crons = re.findall(
            r"(?m)^\s*-\s*cron:\s*['\"]([^'\"]+)['\"]",
            self.text,
        )

        self.assertEqual(crons, ["0 11 * * 1-5"])
        self.assertIn(
            "北京时间 19:00 (UTC 11:00)",
            self.text,
        )

    def test_proven_xshg_trading_day_allows_analysis(self):
        result = self._run_gate(trading_day=True)
        self.assertEqual(result["allow_analysis"], "true")

    def test_false_none_and_calendar_exception_all_skip(self):
        cases = (
            ("closed", False, False),
            ("unknown", None, False),
            ("exception", None, True),
        )

        for name, value, raises in cases:
            with self.subTest(name=name):
                result = self._run_gate(
                    trading_day=value,
                    raises=raises,
                )
                self.assertEqual(
                    result["allow_analysis"],
                    "false",
                )

    def test_schedule_gate_cannot_be_bypassed_by_mutable_flag(self):
        gate_source = self._gate_source()

        self.assertIn(
            "allow_analysis = context.is_trading_day is True",
            gate_source,
        )
        self.assertIn(
            "allow_analysis = False",
            gate_source,
        )
        self.assertIn(
            "except Exception as exc:",
            gate_source,
        )
        self.assertNotIn(
            "TRADING_DAY_CHECK_ENABLED",
            gate_source,
        )

        analysis_start = self.text.index(
            "- name: 执行股票分析"
        )
        analysis_end = self.text.index(
            "- name: 上传分析报告",
            analysis_start,
        )
        analysis_block = self.text[
            analysis_start:analysis_end
        ]

        self.assertIn(
            "github.event_name != 'schedule' || "
            "steps.strict_cn_schedule_gate.outputs."
            "allow_analysis == 'true'",
            analysis_block,
        )
        self.assertIn("SKIP_NO_PUSH", self.text)

    def test_manual_force_run_semantics_are_preserved(self):
        self.assertIn("workflow_dispatch:", self.text)
        self.assertIn("force_run:", self.text)
        self.assertIn(
            "github.event.inputs.force_run",
            self.text,
        )
        self.assertIn(
            'FORCE_RUN_ARG="--force-run"',
            self.text,
        )
        self.assertIn(
            "python main.py $FORCE_RUN_ARG",
            self.text,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)