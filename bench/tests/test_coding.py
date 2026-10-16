"""Coding validation: task scoring, opt-in code execution and the environment rule."""

from __future__ import annotations

import json
from pathlib import Path

from bench.coding import (
    DEFAULT_TASKS,
    RunSummary,
    Task,
    TaskOutcome,
    check_answer,
    load_tasks,
    render,
)


def _task(check: dict, *, code: bool = False) -> Task:
    return Task(
        id="t",
        title="t",
        prompt="p",
        check=check,
        requires_code_execution=code,
    )


def test_the_shipped_task_set_is_valid_and_balanced() -> None:
    tasks = load_tasks(DEFAULT_TASKS)

    assert 10 <= len(tasks) <= 15  # roadmap M6 asks for 10–15 tasks
    assert len({task.id for task in tasks}) == len(tasks)
    kinds = {task.check["kind"] for task in tasks}
    assert {"contains_all", "regex", "json_schema", "one_of", "equals_normalized", "pytest"} <= kinds
    assert sum(1 for task in tasks if task.requires_code_execution) == 2


def test_contains_all_requires_every_fragment_and_forbids_fences() -> None:
    task = _task({"kind": "contains_all", "needles": ["off-by-one", "items[0:len(items)]"], "forbid": ["```"]})

    assert check_answer(task, "This is off-by-one, use items[0:len(items)].", allow_code_execution=False)[0]
    assert not check_answer(task, "off-by-one", allow_code_execution=False)[0]
    assert not check_answer(
        task, "off-by-one items[0:len(items)] ```", allow_code_execution=False
    )[0]


def test_json_schema_accepts_prose_around_the_object_but_checks_values() -> None:
    task = _task(
        {
            "kind": "json_schema",
            "schema": {"tests": "int", "failures": "int", "first_failure": "str"},
            "equals": {"tests": 42, "failures": 3},
        }
    )

    assert check_answer(task, '{"tests": 42, "failures": 3, "first_failure": "x"}', allow_code_execution=False)[0]
    assert check_answer(
        task, 'Here you go: {"tests": 42, "failures": 3, "first_failure": "x"}', allow_code_execution=False
    )[0]
    assert not check_answer(
        task, '{"tests": 41, "failures": 3, "first_failure": "x"}', allow_code_execution=False
    )[0]
    assert not check_answer(task, "no json here", allow_code_execution=False)[0]


def test_one_of_and_equals_normalized_are_strict() -> None:
    choice = _task({"kind": "one_of", "allowed": ["MISSING_FIELD"]})
    equal = _task({"kind": "equals_normalized", "expected": "import os\nimport sys"})

    assert check_answer(choice, "MISSING_FIELD", allow_code_execution=False)[0]
    assert not check_answer(choice, "MISSING_FIELD because…", allow_code_execution=False)[0]
    assert check_answer(equal, "import os\nimport sys\n", allow_code_execution=False)[0]


def test_regex_kind_supports_flags_and_length_caps() -> None:
    task = _task({"kind": "regex", "pattern": "^fix: .*rank.*$", "flags": ["IGNORECASE", "MULTILINE"], "max_length": 20})

    assert check_answer(task, "fix: skip rank 1", allow_code_execution=False)[0]
    assert not check_answer(task, "fix: skip rank 1 and everything else", allow_code_execution=False)[0]


def test_pytest_tasks_are_opt_in_and_execute_when_allowed() -> None:
    task = _task(
        {
            "kind": "pytest",
            "test_source": "from solution import last_page\n\ndef test_last_page():\n"
            "    assert last_page([1, 2, 3, 4], 3) == [4]\n"
            "    assert last_page([], 3) == []\n",
        },
        code=True,
    )

    skipped = check_answer(task, "def last_page(items, size):\n    return items[-1:]", allow_code_execution=False)
    assert not skipped[0]
    assert "not allowed" in skipped[1]

    # The task's own definition of "last page": the trailing partial page, or a full page when
    # the length divides evenly (`last_page([1, 2, 3], 3) == [1, 2, 3]`).
    good_solution = (
        "def last_page(items, size):\n"
        "    remainder = len(items) % size\n"
        "    keep = remainder if remainder else min(size, len(items))\n"
        "    return items[len(items) - keep:] if keep else []"
    )
    good = check_answer(task, good_solution, allow_code_execution=True)
    assert good[0], good[1]

    bad = check_answer(task, "def last_page(items, size):\n    return items", allow_code_execution=True)
    assert not bad[0]
    assert "pytest failed" in bad[1]


def test_render_marks_non_hardware_runs_as_not_a_verdict() -> None:
    summary = RunSummary(outcomes=[TaskOutcome(task=_task({"kind": "one_of", "allowed": ["A"]}), passed=True, reason="ok")])

    mock = render(summary, environment="mock", model="m")
    hardware = render(summary, environment="hardware", model="m")

    assert "Not a verdict" in mock
    assert "Not a verdict" not in hardware
    assert "passed: **1/1**" in hardware


def test_load_tasks_reads_the_repository_file() -> None:
    payload = json.loads(Path(DEFAULT_TASKS).read_text(encoding="utf-8"))

    assert payload["version"] == 1
    assert all("prompt" in task and "check" in task for task in payload["tasks"])
