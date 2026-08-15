"""ArenaHero-Guide-Reforged 中文 JSONL 日志和离线查询工具。"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Iterator, Mapping
from uuid import UUID, uuid4


LOG_FILE_NAME = "arena_core_agent.jsonl"
LOG_MAX_BYTES = 4 * 1024 * 1024
LOG_BACKUP_COUNT = 4
LEVEL_NAMES = {logging.INFO: "信息", logging.WARNING: "警告", logging.ERROR: "错误"}
EVENT_PREFIXES = (
    ("CORE_", "Core 事件"),
    ("UNIT_", "Unit 事件"),
    ("WORKER_", "Worker 经济事件"),
    ("HARVEST_", "Worker 经济事件"),
    ("DEPOSIT_", "Worker 经济事件"),
    ("SHOT_", "战斗事件"),
    ("SWEEP_", "战斗事件"),
    ("DESTRUCTION_", "战斗事件"),
    ("BEACON_", "信标事件"),
    ("RESPAWN_", "重生事件"),
    ("MOVE_", "移动事件"),
)


def new_run_id() -> str:
    return datetime.now().astimezone().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:8]


def _json_value(value: Any) -> Any:
    """只转换日志需要的值，避免把 SDK 对象或凭据写入文件。"""
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _safe_text(value: Any) -> str:
    """去掉换行并遮蔽常见的凭据键，防止一条异常污染 JSONL。"""
    text = str(value).replace("\r", " ").replace("\n", " ")
    patterns = (
        r"(ARENA_HERO_API_KEY=)[^\s,;]+",
        r"(Authorization:)[^\s,;]+",
        r"(Bearer )[^\s,;]+",
    )
    for pattern in patterns:
        text = re.sub(pattern, r"\1<已隐藏>", text)
    return text


def event_category(event_type: str) -> str:
    for prefix, category in EVENT_PREFIXES:
        if event_type.startswith(prefix):
            return category
    return "官方事件"


def event_message(event_type: str, reason_code: str | None = None) -> str:
    category = event_category(event_type)
    if reason_code:
        return f"{category}：{event_type}（原因：{reason_code}）"
    return f"{category}：{event_type}"


def make_record(
    category: str,
    message: str,
    *,
    run_id: str,
    mode: str,
    tick: int | None = None,
    data: Mapping[str, Any] | None = None,
    level: str = "信息",
    timestamp: str | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "时间": timestamp or datetime.now().astimezone().isoformat(timespec="milliseconds"),
        "级别": level,
        "类别": category,
        "消息": _safe_text(message),
        "运行ID": run_id,
        "模式": mode,
    }
    if tick is not None:
        record["Tick"] = tick
    if data:
        record["数据"] = _json_value(data)
    return record


class ChineseJsonFormatter(logging.Formatter):
    """将结构化记录写成单行 UTF-8 JSON；兼容没有结构化数据的旧调用。"""

    def format(self, record: logging.LogRecord) -> str:
        structured = getattr(record, "structured_record", None)
        if structured is None:
            structured = {
                "时间": datetime.now().astimezone().isoformat(timespec="milliseconds"),
                "级别": LEVEL_NAMES.get(record.levelno, "信息"),
                "类别": "运行",
                "消息": _safe_text(record.getMessage()),
            }
        return json.dumps(structured, ensure_ascii=False, separators=(",", ":"))


def configure_logger(log_path: Path, *, run_id: str, mode: str) -> logging.Logger:
    """创建按大小轮转的中文 JSONL logger。"""
    logger = logging.getLogger(f"arena_core_agent.{run_id}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.handlers.clear()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_path,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setFormatter(ChineseJsonFormatter())
    logger.addHandler(handler)
    logger.run_id = run_id  # type: ignore[attr-defined]
    logger.mode = mode  # type: ignore[attr-defined]
    return logger


def write_record(
    logger: logging.Logger,
    category: str,
    message: str,
    *,
    tick: int | None = None,
    data: Mapping[str, Any] | None = None,
    error: bool = False,
) -> None:
    level = logging.ERROR if error else logging.INFO
    record = make_record(
        category,
        message,
        run_id=getattr(logger, "run_id", "未知运行"),
        mode=getattr(logger, "mode", "未知模式"),
        tick=tick,
        data=data,
        level=LEVEL_NAMES[level],
    )
    logger.log(level, record["消息"], extra={"structured_record": record})


def event_record_data(event: Any, *, agent_destination: Any = None) -> dict[str, Any]:
    data: dict[str, Any] = {
        "事件ID": event.event_id,
        "事件类型": event.event_type,
        "原因代码": event.reason_code,
        "执行者ID": event.actor_id,
        "目标ID": event.target_id,
        "位置": event.position,
        "事件值": event.values,
    }
    if agent_destination is not None:
        data["Agent目的格"] = agent_destination
    return data


def iter_log_records(log_dir: Path) -> Iterator[dict[str, Any]]:
    def rotation_age(path: Path) -> int:
        if path.name == LOG_FILE_NAME:
            return 0
        suffix = path.name.removeprefix(f"{LOG_FILE_NAME}.")
        return int(suffix) if suffix.isdigit() else 0

    paths = sorted(log_dir.glob(f"{LOG_FILE_NAME}*"), key=rotation_age, reverse=True)
    for path in paths:
        if not path.is_file():
            continue
        try:
            with path.open(encoding="utf-8") as stream:
                for line_number, line in enumerate(stream, 1):
                    try:
                        value = json.loads(line)
                    except json.JSONDecodeError:
                        print(
                            f"警告：跳过损坏日志 {path.name}:{line_number}",
                            file=sys.stderr,
                        )
                        continue
                    if isinstance(value, dict):
                        yield value
        except OSError as exc:
            print(f"警告：无法读取日志 {path}: {_safe_text(exc)}", file=sys.stderr)


def _tick_in_range(record: Mapping[str, Any], from_tick: int | None, to_tick: int | None) -> bool:
    tick = record.get("Tick")
    if type(tick) is not int:
        return False
    return (from_tick is None or tick >= from_tick) and (to_tick is None or tick <= to_tick)


def filter_events(
    records: Iterable[Mapping[str, Any]],
    *,
    tick: int | None = None,
    event_types: set[str] | None = None,
    reason: str | None = None,
    actor: str | None = None,
) -> list[dict[str, Any]]:
    result = []
    for record in records:
        if record.get("类别") not in {"事件", "官方事件"}:
            continue
        if tick is not None and record.get("Tick") != tick:
            continue
        data = record.get("数据")
        if not isinstance(data, Mapping):
            continue
        if event_types and data.get("事件类型") not in event_types:
            continue
        if reason is not None and data.get("原因代码") != reason:
            continue
        if actor is not None and data.get("执行者ID") != actor:
            continue
        result.append(dict(record))
    return result


def aggregate_stats(
    records: Iterable[Mapping[str, Any]],
    *,
    from_tick: int | None = None,
    to_tick: int | None = None,
) -> dict[str, Any]:
    records = list(records)
    stats = [
        record
        for record in records
        if record.get("类别") == "统计"
        and _tick_in_range(record, from_tick, to_tick)
    ]
    events = filter_events(records, tick=None)
    events = [record for record in events if _tick_in_range(record, from_tick, to_tick)]
    decisions = [
        record
        for record in records
        if record.get("类别") == "决策"
        and _tick_in_range(record, from_tick, to_tick)
    ]
    errors = [
        record
        for record in records
        if record.get("级别") == "错误"
        and _tick_in_range(record, from_tick, to_tick)
    ]
    durations = []
    event_counts: Counter[str] = Counter()
    action_counts: Counter[str] = Counter()
    production_counts: Counter[str] = Counter()
    resources: list[int] = []
    for record in stats:
        data = record.get("数据")
        if not isinstance(data, Mapping):
            continue
        duration = data.get("决策耗时毫秒")
        if isinstance(duration, (int, float)):
            durations.append(float(duration))
        resource = data.get("资源")
        if type(resource) is int:
            resources.append(resource)
        value = data.get("动作数量")
        if isinstance(value, Mapping):
            for name, count in value.items():
                if type(count) is int:
                    action_counts[str(name)] += count
        production = data.get("生产状态")
        if isinstance(production, str):
            production_counts[production] += 1
    for record in events:
        data = record.get("数据")
        if isinstance(data, Mapping) and isinstance(data.get("事件类型"), str):
            event_counts[data["事件类型"]] += 1
    return {
        "统计Tick数": len(stats),
        "决策记录数": len(decisions),
        "错误记录数": len(errors),
        "事件数量": dict(event_counts),
        "动作数量": dict(action_counts),
        "生产状态数量": dict(production_counts),
        "资源起点": resources[0] if resources else None,
        "资源终点": resources[-1] if resources else None,
        "资源变化": resources[-1] - resources[0] if len(resources) >= 2 else 0,
        "平均决策耗时毫秒": round(sum(durations) / len(durations), 3) if durations else 0,
        "最大决策耗时毫秒": round(max(durations), 3) if durations else 0,
        "预算耗尽Tick数": sum(
            1
            for record in stats
            if isinstance(record.get("数据"), Mapping)
            and record["数据"].get("预算耗尽") is True
        ),
    }


def _print_record(record: Mapping[str, Any]) -> None:
    tick = record.get("Tick", "-")
    print(
        f"{record.get('时间', '-')} Tick={tick} "
        f"[{record.get('级别', '信息')}] {record.get('类别', '运行')} "
        f"{record.get('消息', '')}"
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="查询 Arena Hero 中文 JSONL 日志")
    parser.add_argument("--log-dir", type=Path, default=Path(__file__).resolve().parent)
    subparsers = parser.add_subparsers(dest="command", required=True)
    tail = subparsers.add_parser("tail", help="查看最近日志")
    tail.add_argument("--limit", type=int, default=50)
    tail.add_argument("--json", action="store_true", help="输出 JSON")
    events = subparsers.add_parser("events", help="查询官方事件")
    events.add_argument("--tick", type=int)
    events.add_argument("--type", dest="event_types", action="append")
    events.add_argument("--reason")
    events.add_argument("--actor")
    events.add_argument("--limit", type=int, default=100)
    events.add_argument("--json", action="store_true", help="输出 JSON")
    for name, help_text in (("stats", "聚合统计"), ("errors", "查看错误")):
        command = subparsers.add_parser(name, help=help_text)
        command.add_argument("--from-tick", type=int)
        command.add_argument("--to-tick", type=int)
        command.add_argument("--limit", type=int, default=100)
        command.add_argument("--json", action="store_true", help="输出 JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    records = list(iter_log_records(args.log_dir))
    if args.command == "tail":
        selected = [] if args.limit <= 0 else records[-args.limit:]
    elif args.command == "events":
        selected = filter_events(
            records,
            tick=args.tick,
            event_types=set(args.event_types or ()),
            reason=args.reason,
            actor=args.actor,
        )
        selected = [] if args.limit <= 0 else selected[-args.limit:]
    elif args.command == "errors":
        selected = [
            record
            for record in records
            if record.get("级别") == "错误" or record.get("类别") == "错误"
        ]
        selected = [] if args.limit <= 0 else selected[-args.limit:]
    else:
        summary = aggregate_stats(
            records,
            from_tick=args.from_tick,
            to_tick=args.to_tick,
        )
        if args.json:
            print(json.dumps(summary, ensure_ascii=False, indent=2))
        else:
            for key, value in summary.items():
                print(f"{key}：{json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value}")
        return 0
    if args.json:
        print(json.dumps(selected, ensure_ascii=False, indent=2))
    else:
        for record in selected:
            _print_record(record)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
