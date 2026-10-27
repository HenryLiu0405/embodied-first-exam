"""Assertions over raw host traces, independent of benchmark result aggregation."""

import json


def records(directory):
    return [json.loads(line) for line in (directory / "events.jsonl").read_text().splitlines()]


def model_records(directory):
    return [row for row in records(directory) if row["event"] == "span.end" and row["kind"] == "model"]


def check_trace(directory):
    rows = records(directory)
    starts = {row["span_id"] for row in rows if row["event"] == "span.start"}
    ends = [row["span_id"] for row in rows if row["event"] == "span.end"]
    assert starts == set(ends)
    assert len(ends) == len(set(ends))
    assert json.loads((directory / "run.json").read_text())["status"] != "running"
    return True
