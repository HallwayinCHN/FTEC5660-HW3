import json
import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call, patch

import hw3


class FinancialEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.executescript("""
            CREATE TABLE company (industry TEXT);
            CREATE TABLE industry_medians (
                industry TEXT, revenue_growth_2y_pct REAL, operating_margin_pct REAL
            );
            CREATE TABLE orders (order_date TEXT, amount REAL);
            CREATE TABLE monthly_costs (
                month TEXT, cost_of_sales REAL, selling_admin REAL, research_development REAL
            );
            INSERT INTO company VALUES ('target industry');
            INSERT INTO industry_medians VALUES ('target industry', 30, 10), ('other', 90, 90);
            INSERT INTO orders VALUES ('2023-01-01', 100), ('2025-12-31', 130),
                ('2022-12-31', 900), ('2024-01-01', 900), ('2026-01-01', 900);
        """)
        self.db.executemany(
            "INSERT INTO monthly_costs VALUES (?, 6, 2, 1)",
            [(f"2025-{month:02d}",) for month in range(1, 13)],
        )

    def findings(self):
        def invoke(args):
            cursor = self.db.execute(args["query"])
            lines = [" | ".join(column[0] for column in cursor.description)]
            for row in cursor:
                lines.append(" | ".join(
                    f"{value:.2f}" if isinstance(value, float) else str(value) for value in row
                ))
            return "\n".join(lines)

        return hw3.financial_findings(SimpleNamespace(invoke=invoke))

    def test_full_years_company_industry_and_strict_threshold(self):
        points, summary = self.findings()
        self.assertEqual(points, {1: "FAIL", 5: "PASS"})
        self.assertIn("30.00% versus the industry median of 30.00%", summary[0])
        self.assertIn("16.92% versus the industry median of 10.00%", summary[1])

    def test_verdict_uses_unrounded_values(self):
        self.db.execute("UPDATE orders SET amount = 130.001 WHERE order_date = '2025-12-31'")
        points, summary = self.findings()
        self.assertEqual(points[1], "PASS")
        self.assertIn("30.00% versus the industry median of 30.00%", summary[0])

    def test_duplicate_or_missing_cost_month_only_affects_margin(self):
        for query in (
            "INSERT INTO monthly_costs VALUES ('2025-01', 6, 2, 1)",
            "DELETE FROM monthly_costs WHERE month = '2025-01'",
        ):
            with self.subTest(query=query):
                self.db.execute("SAVEPOINT test_costs")
                self.db.execute(query)
                self.assertEqual(self.findings()[0], {1: "FAIL"})
                self.db.execute("ROLLBACK TO test_costs")
                self.db.execute("RELEASE test_costs")

    def test_zero_sales_baseline_only_affects_growth(self):
        self.db.execute("UPDATE orders SET amount = 0 WHERE order_date = '2023-01-01'")
        self.assertEqual(self.findings()[0], {5: "PASS"})


def model_response(verdict, quote):
    return SimpleNamespace(content=json.dumps({"verdict": verdict, "quote": quote}))


class InterviewEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_key_preserves_financial_points_without_spending_interview(self):
        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": ""}):
            agent = hw3.build_agent()

        sql = Mock()
        sql.invoke.return_value = "growth | benchmark | pass | margin | benchmark | pass\n31 | 30 | 1 | 9 | 10 | 0"
        interview = Mock()
        analyses = await hw3.analyze_companies(agent, {"example": [None, None, sql, interview]})

        self.assertEqual(analyses["example"]["points"], {1: "PASS", 5: "FAIL"})
        self.assertIn("DeepSeek API key was unavailable", analyses["example"]["summary"])
        interview.invoke.assert_not_called()

    async def test_valid_current_employee_quote_uses_one_interview(self):
        interview = Mock()
        interview.invoke.return_value = "Managers follow up promptly on staff concerns."
        model = SimpleNamespace(ainvoke=AsyncMock(return_value=model_response(
            "PASS", "follow up promptly on staff concerns"
        )))

        verdict, evidence = await hw3.employee_finding(model, interview)

        self.assertEqual(verdict, "PASS")
        self.assertIn("follow up promptly on staff concerns", evidence)
        interview.invoke.assert_called_once_with({"role": "current_employee", "topic": "working_conditions"})

    async def test_invalid_model_replies_trigger_validated_fallback(self):
        interview = Mock()
        interview.invoke.side_effect = [
            "I have little contact with management.",
            "Experienced staff left after management ignored complaints.",
        ]
        model = SimpleNamespace(ainvoke=AsyncMock(side_effect=[
            model_response("PASS", "Invented evidence"),
            model_response("FAIL", "Also invented"),
            model_response("FAIL", "management ignored complaints"),
        ]))

        verdict, evidence = await hw3.employee_finding(model, interview)

        self.assertEqual(verdict, "FAIL")
        self.assertIn("former employee", evidence)
        self.assertEqual(interview.invoke.call_args_list, [
            call({"role": "current_employee", "topic": "working_conditions"}),
            call({"role": "former_employee", "topic": "working_conditions"}),
        ])
        self.assertEqual(model.ainvoke.await_count, 3)

    async def test_inconclusive_interviews_leave_no_unsupported_verdict(self):
        interview = Mock()
        interview.invoke.return_value = "I cannot comment."
        model = SimpleNamespace(ainvoke=AsyncMock(return_value=model_response("INSUFFICIENT", "")))

        verdict, evidence = await hw3.employee_finding(model, interview)

        self.assertIsNone(verdict)
        self.assertEqual(interview.invoke.call_count, 2)
        self.assertIn("Neither employee interview yielded a validated verdict", evidence)

    async def test_abstention_does_not_rely_on_the_quote_field(self):
        transcript = "I have only worked two shifts and cannot comment on employee relations."
        for payload in (
            {"verdict": "INSUFFICIENT", "quote": transcript},
            {"verdict": "INSUFFICIENT"},
        ):
            with self.subTest(payload=payload):
                reply = SimpleNamespace(content=json.dumps(payload))
                model = SimpleNamespace(ainvoke=AsyncMock(return_value=reply))
                self.assertEqual(await hw3.classify_interview(model, transcript), ("INSUFFICIENT", ""))
                model.ainvoke.assert_awaited_once()
