from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from uuid import UUID

from arena_hero import ResolutionEvent

import arena_log
import arena_core_agent as agent


class ArenaLogTests(unittest.TestCase):
    def record(self, category, message, *, tick=None, data=None, **kwargs):
        return arena_log.make_record(
            category,
            message,
            run_id="run-test",
            mode="control",
            tick=tick,
            data=data,
            timestamp="2026-08-16T00:00:00+08:00",
            **kwargs,
        )

    def test_record_is_chinese_json_safe_and_hides_api_key(self):
        record = self.record(
            "错误",
            "请求失败 ARENA_HERO_API_KEY=secret-value; 请重试",
            level="错误",
        )

        encoded = json.dumps(record, ensure_ascii=False)
        self.assertIn("错误", encoded)
        self.assertIn("<已隐藏>", encoded)
        self.assertNotIn("secret-value", encoded)

    def test_event_record_keeps_unknown_official_fields(self):
        event = ResolutionEvent(
            event_id=UUID(int=10),
            tick=42,
            event_type="A_FUTURE_EVENT",
            reason_code="A_FUTURE_REASON",
            actor_id=UUID(int=11),
            target_id=None,
            position=(3, 4),
            values={"future": 7},
        )

        data = arena_log.event_record_data(event, agent_destination=(5, 4))

        self.assertEqual(data["事件类型"], "A_FUTURE_EVENT")
        self.assertEqual(data["原因代码"], "A_FUTURE_REASON")
        self.assertEqual(data["事件值"], {"future": 7})
        self.assertEqual(data["Agent目的格"], (5, 4))
        self.assertEqual(arena_log.event_category(event.event_type), "官方事件")

    def test_rotated_logs_are_read_oldest_first_and_bad_lines_are_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = self.record("生命周期", "旧记录", tick=1)
            current = self.record("生命周期", "新记录", tick=2)
            (root / "arena_core_agent.jsonl.1").write_text(
                json.dumps(old, ensure_ascii=False) + "\n损坏\n",
                encoding="utf-8",
            )
            (root / "arena_core_agent.jsonl").write_text(
                json.dumps(current, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

            records = list(arena_log.iter_log_records(root))

        self.assertEqual([record["Tick"] for record in records], [1, 2])

    def test_filter_events_and_aggregate_stats(self):
        records = [
            self.record(
                "统计",
                "Tick 统计",
                tick=10,
                data={
                    "资源": 5,
                    "决策耗时毫秒": 2.5,
                    "动作数量": {"移动": 1},
                    "事件数量": {"UNIT_MOVE_FAILED": 2},
                    "预算耗尽": False,
                },
            ),
            self.record(
                "事件",
                "移动失败",
                tick=10,
                data={"事件类型": "UNIT_MOVE_FAILED", "原因代码": "CELL_UNIT_LIMIT", "执行者ID": "u1"},
            ),
            self.record(
                "统计",
                "Tick 统计",
                tick=11,
                data={
                    "资源": 8,
                    "决策耗时毫秒": 4.5,
                    "动作数量": {"移动": 2},
                    "预算耗尽": True,
                },
            ),
            self.record("事件", "移动失败", tick=11, data={"事件类型": "UNIT_MOVE_FAILED", "原因代码": "CELL_UNIT_LIMIT", "执行者ID": "u2"}),
        ]

        events = arena_log.filter_events(
            records,
            event_types={"UNIT_MOVE_FAILED"},
            reason="CELL_UNIT_LIMIT",
        )
        stats = arena_log.aggregate_stats(records, from_tick=10, to_tick=11)

        self.assertEqual(len(events), 2)
        self.assertEqual(stats["统计Tick数"], 2)
        self.assertEqual(stats["资源变化"], 3)
        self.assertEqual(stats["动作数量"]["移动"], 3)
        self.assertEqual(stats["事件数量"]["UNIT_MOVE_FAILED"], 2)
        self.assertEqual(stats["预算耗尽Tick数"], 1)
        self.assertEqual(stats["最大决策耗时毫秒"], 4.5)

    def test_cli_events_and_stats_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            records = [
                self.record("事件", "移动失败", tick=7, data={"事件类型": "UNIT_MOVE_FAILED", "原因代码": "MOVE_CONTESTED"}),
                self.record("统计", "Tick 统计", tick=7, data={"资源": 3, "决策耗时毫秒": 1}),
            ]
            (root / arena_log.LOG_FILE_NAME).write_text(
                "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n",
                encoding="utf-8",
            )
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    arena_log.main(
                        ["--log-dir", str(root), "events", "--tick", "7", "--json"]
                    ),
                    0,
                )
            self.assertIn("UNIT_MOVE_FAILED", output.getvalue())

            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    arena_log.main(["--log-dir", str(root), "stats", "--json"]),
                    0,
                )
            self.assertIn("统计Tick数", output.getvalue())

    def test_runtime_logger_writes_jsonl_and_keeps_move_origin_separate(self):
        event = ResolutionEvent(
            event_id=UUID(int=20),
            tick=9,
            event_type="UNIT_MOVE_FAILED",
            reason_code="CELL_UNIT_LIMIT",
            actor_id=UUID(int=21),
            position=(1, 1),
        )
        memory = agent.AgentMemory(
            pending_move_tick=9,
            pending_move_destinations={UUID(int=21): (2, 1)},
        )
        turn = type("EventTurn", (), {"events": (event,)})()

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / arena_log.LOG_FILE_NAME
            logger = arena_log.configure_logger(
                path,
                run_id="run-runtime",
                mode="control",
            )
            agent.log_turn_events(logger, turn, memory)
            for handler in logger.handlers:
                handler.flush()
            record = next(arena_log.iter_log_records(Path(directory)))
            for handler in list(logger.handlers):
                handler.close()
                logger.removeHandler(handler)

        self.assertEqual(record["类别"], "事件")
        self.assertEqual(record["数据"]["位置"], [1, 1])
        self.assertEqual(record["数据"]["Agent目的格"], [2, 1])
        self.assertEqual(record["数据"]["原因代码"], "CELL_UNIT_LIMIT")


if __name__ == "__main__":
    unittest.main()
