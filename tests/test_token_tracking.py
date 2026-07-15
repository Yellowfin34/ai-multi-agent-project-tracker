import importlib.util
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast


APP_PATH = Path(__file__).resolve().parents[1] / "app.py"


def load_app() -> Any:
    spec = importlib.util.spec_from_file_location("project_tracker_app", APP_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load app module from {APP_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return cast(Any, module)


class TokenTrackingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = load_app()
        self.app.DB_PATH = Path(self.tmp.name) / "project_tracker.db"
        self.app.TOKEN_PATH = Path(self.tmp.name) / "agent_tokens.json"
        self.app.init_db()

    def test_event_records_cached_and_reasoning_tokens(self):
        with self.app.connect() as conn:
            project_id = conn.execute("SELECT id FROM projects ORDER BY id LIMIT 1").fetchone()[0]
            self.app.event(
                conn,
                project_id,
                "admin",
                "project_update_report",
                "reported usage",
                input_tokens=1000,
                cached_input_tokens=800,
                output_tokens=250,
                reasoning_tokens=50,
                total_tokens=1250,
                model="openai/gpt-5.5",
                commit_refs=["abc123 token tracking"],
            )
            row = conn.execute("SELECT * FROM project_events ORDER BY id DESC LIMIT 1").fetchone()

        event = self.app.event_from_row(row)
        self.assertEqual(event["input_tokens"], 1000)
        self.assertEqual(event["cached_input_tokens"], 800)
        self.assertEqual(event["output_tokens"], 250)
        self.assertEqual(event["reasoning_tokens"], 50)
        self.assertEqual(event["total_tokens"], 1250)
        self.assertEqual(event["model"], "openai/gpt-5.5")
        self.assertEqual(event["commit_refs"], ["abc123 token tracking"])

    def test_cached_input_tokens_are_clamped_to_input_tokens_for_cost(self):
        event = self.app.event_from_row({
            "input_tokens": 100,
            "cached_input_tokens": 500,
            "output_tokens": 10,
            "reasoning_tokens": 3,
            "total_tokens": 110,
            "commit_refs_json": "[]",
        })

        self.assertEqual(event["cached_input_tokens"], 100)
        expected = round((100 / 1_000_000 * 0.50) + (10 / 1_000_000 * 30.00), 6)
        self.assertEqual(event["estimated_cost_usd"], expected)

    def test_token_summary_aggregates_cached_and_reasoning_tokens(self):
        with self.app.connect() as conn:
            project_id = conn.execute("SELECT id FROM projects ORDER BY id LIMIT 1").fetchone()[0]
            self.app.event(conn, project_id, "admin", "project_update_report", "first", input_tokens=1000, cached_input_tokens=250, output_tokens=100, reasoning_tokens=10, total_tokens=1100)
            self.app.event(conn, project_id, "admin", "project_update_report", "second", input_tokens=2000, cached_input_tokens=1500, output_tokens=200, reasoning_tokens=20, total_tokens=2200)
            day = self.app.now_iso()[:10]
            totals = dict(conn.execute("SELECT COALESCE(SUM(input_tokens),0) input_tokens, COALESCE(SUM(cached_input_tokens),0) cached_input_tokens, COALESCE(SUM(output_tokens),0) output_tokens, COALESCE(SUM(reasoning_tokens),0) reasoning_tokens, COALESCE(SUM(total_tokens),0) total_tokens, COUNT(*) updates FROM project_events WHERE substr(created_at,1,10)=? AND total_tokens>0", (day,)).fetchone())

        self.assertEqual(totals["input_tokens"], 3000)
        self.assertEqual(totals["cached_input_tokens"], 1750)
        self.assertEqual(totals["output_tokens"], 300)
        self.assertEqual(totals["reasoning_tokens"], 30)
        self.assertEqual(totals["total_tokens"], 3300)
        self.assertEqual(totals["updates"], 2)


if __name__ == "__main__":
    unittest.main()
