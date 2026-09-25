"""The verification scenario is deterministic and matches the committed transcript;
the inspection commands work on its database."""

from __future__ import annotations

from pathlib import Path

import pytest

from insta_outreach.cli import main
from insta_outreach.verification import EXPECTED_SCENARIO, normalise, run_scenario


@pytest.fixture(scope="module")
def scenario(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, list[str], list[str]]:
    import asyncio

    data_dir = tmp_path_factory.mktemp("scenario")
    lines, failed = asyncio.run(run_scenario(data_dir, echo=False))
    return data_dir, lines, failed


def test_scenario_passes_every_check(scenario) -> None:
    _, lines, failed = scenario
    assert failed == [], failed
    assert lines[-1].startswith("RESULT: ") and lines[-1].endswith("checks passed")


def test_scenario_matches_expected_transcript(scenario) -> None:
    data_dir, lines, _ = scenario
    expected = EXPECTED_SCENARIO.read_text(encoding="utf-8").splitlines()
    assert normalise(lines, data_dir) == expected


@pytest.mark.parametrize(
    ("argv", "must_contain"),
    [
        (["leads"], "@sim.smileline.dental"),
        (["leads", "--status", "DISQUALIFIED"], "personal profile: no business signals"),
        (["generated", "all"], "This is Rohit from LemmeDeliver."),
        (["messages", "@sim.aroma.kitchen.mulund"], "Not interested, thanks"),
        (["actions", "--type", "SEND_FOLLOW_UP"], "SEND_FOLLOW_UP"),
        (["suppressions"], "9000000016"),
        (["conversations", "--paused"], "human replied from the Instagram app"),
        (["incidents", "--all"], "CHECKPOINT_REQUIRED"),
        (["audit", "--kind", "lane"], "lane.halted"),
        (["explain", "@sim.skinsense.derma"], "private reply to their comment"),
        (["explain", "@sim.pearl.dental.khar"], "duplicate of @sim.pearl.dental.bandra"),
        (["safety"], "new conversations: 5/day  (runtime override), 2/hour  (runtime override)"),
        (["preflight"], "simulation: no live prerequisites apply"),
    ],
)
def test_inspection_commands_on_the_scenario_database(scenario, capsys, argv, must_contain) -> None:
    data_dir, _, _ = scenario
    code = main(["--config", str(data_dir / "settings.yaml"), *argv])
    out = capsys.readouterr().out
    assert code == 0, out
    assert must_contain in out, out[-3000:]


def test_action_report_shows_gate_decisions_and_attempts(scenario, capsys) -> None:
    data_dir, _, _ = scenario
    config = str(data_dir / "settings.yaml")
    assert main(["--config", config, "actions", "--type", "SEND_OUTREACH", "--status", "SUCCEEDED"]) == 0
    action_id = next(tok for tok in capsys.readouterr().out.split() if tok.startswith("act_"))
    assert main(["--config", config, "action", action_id]) == 0
    report = capsys.readouterr().out
    assert "gate at proposal: ALLOW" in report and "attempt 1" in report and "SUCCESS" in report


async def test_demo_narrates_the_checkpoint(tmp_path: Path) -> None:
    from insta_outreach.verification import run_demo

    lines = await run_demo(tmp_path / "demo", days=2, checkpoint=True, echo=False)
    text = "\n".join(lines)
    assert "lane.halted" in text and "CHECKPOINT - what to observe" in text
    assert "browser operations since the halt: 0 (must be 0)" in text
    assert "released 1 action(s)" in text


@pytest.mark.browser
async def test_browser_demo_uses_the_real_agent_and_stops_at_the_checkpoint(tmp_path: Path) -> None:
    from insta_outreach.verification import run_browser_demo

    lines = await run_browser_demo(tmp_path / "bdemo", echo=False)
    text = "\n".join(lines)
    assert "mock Instagram received a DM to @mock.the.brew.room" in text
    assert "ALREADY_CONTACTED thread_has_history" in text  # Rohit's earlier message is respected
    assert "browser lane: HALTED (CHECKPOINT_REQUIRED" in text
    assert "checkpoint_required.png" in text
