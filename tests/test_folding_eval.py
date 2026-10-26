from __future__ import annotations

import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from agents.agent import Agent
from agents.eval_folding import _run_case, extract_answer, load_cases, score_answer, summarize


class FoldingSwitchTests(unittest.TestCase):
    def _agent(self, **kwargs) -> Agent:
        with patch("agents.agent.build_system_prompt", return_value="test system prompt"):
            return Agent(api_base="http://example.test/v1", api_key="test", **kwargs)

    def test_disabled_folding_blocks_all_entry_points(self) -> None:
        agent = self._agent(enable_folding=False, auto_compact_threshold=0.01)
        self.assertNotIn("compact_context", [tool["name"] for tool in agent.tools])
        agent.last_input_token_count = agent.effective_window
        with patch.object(agent, "_compact_conversation", new_callable=AsyncMock) as compact:
            asyncio.run(agent._check_and_compact())
            compact.assert_not_awaited()
        self.assertFalse(asyncio.run(agent._compact_conversation(trigger="manual")))
        self.assertIn("disabled", asyncio.run(agent._execute_compact_context_tool({})))
        self.assertIn("not available", asyncio.run(agent._execute_tool_call("compact_context", {})))
        self.assertEqual(agent._fold_count, 0)

    def test_enabled_folding_replaces_history_and_records_trigger(self) -> None:
        fold_events = []
        agent = self._agent(enable_folding=True, fold_observer=fold_events.append)
        agent._openai_messages.extend([
            {"role": "user", "content": "Remember blue."},
            {"role": "assistant", "content": "OK"},
            {"role": "user", "content": "What next?"},
        ])
        memory = {
            "episode_memory": {"task_description": "Remember blue", "key_events": [], "current_progress": ""},
            "working_memory": {"immediate_goal": "Answer", "current_challenges": "", "next_actions": []},
            "tool_memory": {"tools_used": [], "derived_rules": []},
        }
        with patch.object(agent, "_generate_folded_session_memory", new_callable=AsyncMock, return_value=memory), patch("agents.agent.save_folded_session_memory"):
            self.assertTrue(asyncio.run(agent._compact_conversation(trigger="eval_scheduled")))
        self.assertEqual(agent._fold_count, 1)
        self.assertEqual(agent._folded_session_memories[0]["trigger"], "eval_scheduled")
        self.assertIn("Remember blue", fold_events[0]["transcript"])
        self.assertEqual(len(agent._openai_messages), 2)

    def test_fold_limit_blocks_a_second_fold(self) -> None:
        agent = self._agent(max_fold_limit=1)
        agent._openai_messages.extend([
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "two"},
            {"role": "user", "content": "three"},
        ])
        with patch.object(agent, "_generate_folded_session_memory", new_callable=AsyncMock, return_value={}), patch("agents.agent.save_folded_session_memory"):
            self.assertTrue(asyncio.run(agent._compact_conversation()))
            self.assertFalse(asyncio.run(agent._compact_conversation()))
        self.assertIn("limit reached", asyncio.run(agent._execute_compact_context_tool({})))
        self.assertEqual(agent._fold_count, 1)

    def test_three_memories_are_independent_calls(self) -> None:
        agent = self._agent(fold_memory_mode="three_parallel")
        responses = iter([
            '{"task_description":"task","key_events":[],"current_progress":"done"}',
            '{"immediate_goal":"answer","current_challenges":"","next_actions":[]}',
            '{"tools_used":[],"derived_rules":["read first"]}',
        ])
        calls = []
        async def side_query(system, prompt):
            calls.append((system, prompt))
            return next(responses)
        with patch.object(agent, "_build_side_query", return_value=side_query):
            memory = asyncio.run(agent._generate_folded_session_memory("conversation"))
        self.assertEqual(len(calls), 3)
        self.assertTrue(all("conversation" in prompt for _, prompt in calls))
        self.assertEqual(memory["episode_memory"]["task_description"], "task")
        self.assertEqual(memory["working_memory"]["immediate_goal"], "answer")
        self.assertEqual(memory["tool_memory"]["derived_rules"], ["read first"])

    def test_malformed_parallel_memory_keeps_transcript(self) -> None:
        agent = self._agent(fold_memory_mode="three_parallel")
        responses = iter(["{}", "{}", "{}"])
        async def side_query(system, prompt):
            return next(responses)
        with patch.object(agent, "_build_side_query", return_value=side_query):
            memory = asyncio.run(agent._generate_folded_session_memory("critical original detail"))
        self.assertIn("critical original detail", memory["episode_memory"]["current_progress"])


class FoldingResultTests(unittest.TestCase):
    def test_case_loading_scoring_and_pair_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "cases.jsonl"
            path.write_text(json.dumps({"id": "task/1", "prompts": ["one", "two", "three"], "answer": "BLUE"}) + "\n", encoding="utf-8")
            cases = load_cases(path)
        self.assertEqual(cases[0]["task_id"], "task/1")
        self.assertEqual(extract_answer("reason\nFINAL_ANSWER: Blue\n"), "Blue")
        self.assertTrue(score_answer("Blue", ["BLUE"], mode="normalized"))
        self.assertFalse(score_answer("Blue", ["BLUE"], mode="exact"))
        rows = [
            {"task_id": "task/1", "condition": condition, "status": "completed", "correct": condition == "fold_on", "fold_count": int(condition == "fold_on"),
             "main_input_tokens": 10, "main_output_tokens": 2, "side_input_tokens": 3, "side_output_tokens": 1}
            for condition in ("fold_on", "fold_off")
        ]
        summary = summarize(rows)
        self.assertEqual(summary["paired_outcomes"]["fold_only"], 1)
        self.assertEqual(summary["cases_with_fold"], 1)
        self.assertEqual(summary["accuracy_delta_percentage_points"], 100)

    def test_paired_runner_writes_isolated_traces(self) -> None:
        class FakeAgent:
            def __init__(self, *, enable_folding: bool, **kwargs) -> None:
                self.enable_folding = enable_folding
                self.session_id = "fake-session"
                self._aborted = False
                self._fold_count = 0
                self._openai_messages = [{"role": "system", "content": "test"}]
                self._anthropic_messages = []
                self._folded_session_memories = []
                self.total_input_tokens = 10
                self.total_output_tokens = 2
                self.side_input_tokens = 0
                self.side_output_tokens = 0
                self.current_turns = 0
                self._mcp_manager = type("Mcp", (), {"disconnect_all": AsyncMock()})()

            async def run_once(self, prompt):
                self._openai_messages.extend([{"role": "user", "content": prompt}, {"role": "assistant", "content": "ACK"}])
                return {"text": "FINAL_ANSWER: BLUE" if "final answer" in prompt else "ACK"}

            async def _compact_conversation(self, *, trigger):
                self._fold_count += 1
                self._folded_session_memories.append({"trigger": trigger})
                return True

            async def drain_background_skill_tasks(self):
                return None

            def abort(self):
                self._aborted = True

            def _get_current_cost_usd(self):
                return 0.0

        case = {"task_id": "task/1", "prompts": ["first", "second", "third"], "answers": ["BLUE"]}
        with tempfile.TemporaryDirectory() as temp_dir, patch("agents.eval_folding.Agent", FakeAgent):
            rows = []
            for condition in ("fold_on", "fold_off"):
                rows.append(asyncio.run(_run_case(
                    case, condition, output_dir=Path(temp_dir), model="fake", api_base="http://example.test/v1",
                    api_key="fake", use_openai=True, fold_threshold=0.7, max_turns=10,
                    max_cost=1.0, timeout=10, score_mode="normalized", fold_after_turn=2,
                )))
            self.assertEqual([row["fold_count"] for row in rows], [1, 0])
            self.assertTrue(all(row["correct"] for row in rows))
            self.assertTrue(all(Path(row["trace_file"]).is_file() for row in rows))
            self.assertTrue(all(Path(row["trace_file"]).is_relative_to(Path(temp_dir)) for row in rows))


if __name__ == "__main__":
    unittest.main()
