import json
import os
from pathlib import Path

import allure
import pytest

from function.api_client import create_client, request
from function.reporting import PROJECT_NAME
from function.response_checks import expect_json
from function.rewards_flow import flow_case, run_flow
from function.rewards_simulation import ADMIN, run_simulation
from function.rewards_test_payout import run_date_test


pytestmark = [
    allure.parent_suite(PROJECT_NAME),
    allure.suite("Rewards"),
    allure.epic(PROJECT_NAME),
    allure.feature("M06 Rewards"),
]

SIMULATION_JOURNAL = Path(".local/rewards/simulation")
FLOW_JOURNAL = Path(".local/rewards")


def report_evidence(result):
    evidence = json.dumps(result, ensure_ascii=False)
    print(evidence)
    allure.attach(evidence, "M06 本轮证据", allure.attachment_type.JSON)


@allure.title("M06 好友进度 · {operation}")
@pytest.mark.parametrize("operation", [
    pytest.param("snapshot", marks=pytest.mark.m06_operation("snapshot")),
    pytest.param("inspect", marks=pytest.mark.m06_operation("inspect")),
    pytest.param("inspect-admin", marks=pytest.mark.m06_operation("inspect-admin")),
    pytest.param("simulate", marks=pytest.mark.m06_operation("simulate")),
])
def test_friend_progress(pytestconfig, operation):
    result = run_simulation(
        operation,
        root=pytestconfig.getoption("--m06-journal") or SIMULATION_JOURNAL,
        run_id=pytestconfig.getoption("--m06-run-id"),
        expected_version=pytestconfig.getoption("--m06-scene-version"),
        report=pytestconfig.getoption("--m06-report"),
    )
    report_evidence(result)
    if operation == "snapshot" and result["task_visible"] is False:
        pytest.skip("当前账号看不到配置的好友任务，已保存 Rewards 账务快照")
    if operation == "simulate":
        assert result["done_count"] == 1 and result["no_payout_verified"] is True


@allure.title("M06 日期工具 · {operation}")
@pytest.mark.parametrize(("operation", "expected_status"), [
    pytest.param("prepare", "prepared", marks=pytest.mark.m06_operation("prepare"), id="prepare"),
    pytest.param("grant", "passed", marks=pytest.mark.m06_operation("grant"), id="grant"),
    pytest.param("reset", "reset", marks=pytest.mark.m06_operation("reset"), id="reset"),
])
def test_date_tool(pytestconfig, operation, expected_status):
    if operation == "prepare" and os.getenv("BUZZING_SKIP_M06_AUTH_SETUP") == "1":
        admin_token = os.getenv("BUZZING_REWARDS_ADMIN_TOKEN", "").strip().removeprefix("Bearer ")
        if not admin_token:
            pytest.skip("M06 后台凭证未配置，跳过定时发奖准备")
        with create_client(bearer_token=admin_token) as client:
            response = request(client, "GET", f"{ADMIN}/tasks", operation="rewards_date_test_preflight")
        if response.status_code in (401, 403):
            pytest.skip(f"M06 后台凭证或访问权限未就绪（HTTP {response.status_code}），跳过定时发奖准备")
        expect_json(response, 200)
    result = run_date_test(
        operation,
        root=pytestconfig.getoption("--m06-journal") or SIMULATION_JOURNAL,
        case_name=pytestconfig.getoption("--m06-case"),
        run_id=pytestconfig.getoption("--m06-run-id"),
        scene_version=pytestconfig.getoption("--m06-scene-version"),
        resume=pytestconfig.getoption("--m06-resume"),
        wait_seconds=pytestconfig.getoption("--m06-wait-seconds"),
    )
    report_evidence(result)
    assert result["status"] == expected_status
    if operation == "grant":
        assert result["repeat_verified"] is True


@allure.title("M06 真实业务事件 · {operation}")
@pytest.mark.parametrize("operation", [
    pytest.param("flow", marks=pytest.mark.m06_operation("flow")),
    pytest.param("replay", marks=pytest.mark.m06_operation("replay")),
])
def test_business_event(pytestconfig, operation):
    case_path = pytestconfig.getoption("--m06-case-file")
    replay_path = pytestconfig.getoption("--m06-replay-event")
    if case_path is None:
        pytest.fail("真实业务事件须通过 --m06-case-file 指定本轮用例", pytrace=False)
    if (replay_path is not None) != (operation == "replay"):
        pytest.fail("仅 replay 操作需要 --m06-replay-event", pytrace=False)
    case = flow_case(case_path)
    root = pytestconfig.getoption("--m06-journal") or FLOW_JOURNAL
    outcome = run_flow(case, root, pytestconfig.getoption("--m06-wait-seconds"), replay_path)
    report_evidence({"scenario": case["scenario"], "status": outcome, "journal": str(root / case["run_id"])})
    if outcome == "waiting_day_2":
        pytest.skip("Day 1 进度已记录，请在下一个 Manila 自然日用原运行 ID 继续")
    assert outcome == ("replay_checked" if operation == "replay" else "paid")
