import json
from copy import deepcopy

import httpx
import pytest

from function import rewards_simulation
from function.rewards_flow import RunJournal
from function.rewards_run import HISTORY, REWARDS, RewardsAPI
from function.rewards_simulation import ADMIN, CASE_NAME, FriendSimulation, verify_friend_task
from function.settings import load_test_data


class PreviewDateTest:
    def __init__(self):
        self.case = {**load_test_data()["rewards_cases"][CASE_NAME],
                     "run_id": "8307eff7-6396-42b9-bbd6-b12b8c82d49b"}
        self.task = self.case["task_id"]
        self.uid = self.case["uid"]
        self.ids = tuple(self.case["friend_conditions"].values())
        self.task_reply = {"id": self.task, "version": "2", "online": True, "config": {
            "type": "friends", "pay_mode": "once", "reward_cents": "500", "friend_limit": 2,
            "unlock": {"kind": "action", "condition": {"action": "trade", "measure": "count", "target": "1000"}},
            "friend_requirements": [
                {"id": key, "condition": {"action": action, "measure": "count", "target": "1",
                                          "first_deposit": action == "deposit"}}
                for action, key in self.case["friend_conditions"].items()
            ],
        }}
        self.state = {"uid": str(self.uid), "task_id": self.task, "active": False, "version": "0",
                      "now": "2026-11-27T04:00:00Z", "friend_projections": []}
        self.requests = []
        self.previous = {}
        self.lose_response = False
        self.changed_credit = False

    def admin(self, request):
        if request.method == "GET":
            if request.url.path.endswith("/tasks"):
                return httpx.Response(200, json={"tasks": [self.task_reply]})
            return httpx.Response(200, json=self.state)
        assert request.url.path == f"{ADMIN}/{self.uid}/{self.task}"
        body = json.loads(request.content)
        key = request.headers["Idempotency-Key"]
        self.requests.append((body, key))
        if key in self.previous:
            previous_body, state = self.previous[key]
            assert body == previous_body
            return httpx.Response(200, json=state)
        assert body["expected_version"] == self.state["version"]
        self.state["version"] = str(int(self.state["version"]) + 1)
        if body["action"] == "create":
            assert body["simulate_unlock"] is True
            self.state["active"] = True
        else:
            assert body["action"] == "set_friends"
            simulation = body["friends"][0]
            assert simulation["invited_on"] == "2026-11-27"
            day = simulation["days"][0]
            assert day["date"] == simulation["invited_on"]
            conditions = day["conditions"]
            assert [condition["id"] for condition in conditions] == list(self.ids)
            self.state["friend_projections"] = [{"key": simulation["key"], "friend": {
                "id": "test:relation", "progress": [
                    {"id": condition["id"], "done": condition["completed"]} for condition in conditions
                ],
            }}]
        self.previous[key] = body, deepcopy(self.state)
        if self.lose_response:
            self.lose_response = False
            raise httpx.ReadTimeout("one server response lost")
        return httpx.Response(200, json=self.state)

    def user(self, request):
        assert request.method == "GET"
        path = request.url.path
        if path == "/api/v1/auth/session":
            return httpx.Response(200, json={"auth_level": 2, "user_id": str(self.uid)})
        if path == f"{REWARDS}/overview":
            return httpx.Response(200, json={"receipt_revision": "17"})
        if path == f"{REWARDS}/receipts":
            assert request.url.params["after_revision"] in {"0", "17"}
            return httpx.Response(200, json={"receipts": [], "has_more": False})
        if path == HISTORY:
            batches = [{"id": "98"}] if self.changed_credit and self.state["version"] == "3" else []
            return httpx.Response(200, json={"batches": batches, "has_more": False})
        if path == "/api/v1/notifications/list":
            return httpx.Response(200, json={"code": 0, "data": {
                "notifications": [], "total": 0, "page": 1, "page_size": 50,
            }})
        context = {"version": self.state["version"]}
        if path == f"{REWARDS}/tasks/{self.task}":
            return httpx.Response(200, json={"test_context": context, "qualified": False})
        assert path == f"{REWARDS}/tasks/{self.task}/friends"
        friends = [row["friend"] for row in self.state["friend_projections"]]
        done = sum(all(step["done"] for step in friend["progress"]) for friend in friends)
        return httpx.Response(200, json={"test_context": context, "friends": friends, "done_count": str(done)})

    def run(self, tmp_path):
        with (httpx.Client(base_url="https://preview-api.buzzing.app", transport=httpx.MockTransport(self.admin)) as admin,
              httpx.Client(base_url="https://preview-api.buzzing.app", transport=httpx.MockTransport(self.user)) as user):
            simulator = FriendSimulation(admin, RewardsAPI(user, self.uid, []), self.case)
            return simulator.run(RunJournal(tmp_path, self.case))


def test_simulation_projects_partial_then_done_without_a_credit_grant(tmp_path):
    preview = PreviewDateTest()
    result = preview.run(tmp_path)
    assert result["done_count"] == 1
    assert not result["task_qualified"]
    assert result["no_payout_verified"]
    assert [body["action"] for body, _ in preview.requests] == ["create", "set_friends", "set_friends"]
    assert preview.task == "8d1c5468b058886d6aa902db2d5f5e99"
    assert [entry["completed"] for entry in preview.requests[1][0]["friends"][0]["days"][0]["conditions"]] == [True, False]
    assert preview.run(tmp_path) == result
    assert len(preview.requests) == 3


def test_documented_friend_task_requires_first_deposit_and_inviter_unlock():
    preview = PreviewDateTest()
    verify_friend_task(preview.task_reply, preview.case)
    deposit = next(row["condition"] for row in preview.task_reply["config"]["friend_requirements"]
                   if row["condition"]["action"] == "deposit")
    deposit["first_deposit"] = False
    with pytest.raises(ValueError, match="首充"):
        verify_friend_task(preview.task_reply, preview.case)
    deposit["first_deposit"] = True
    preview.task_reply["config"]["unlock"]["condition"]["target"] = "1"
    with pytest.raises(ValueError, match="解锁规则"):
        verify_friend_task(preview.task_reply, preview.case)


def test_simulation_resumes_with_the_original_admin_idempotency_key(tmp_path):
    preview = PreviewDateTest()
    preview.lose_response = True
    with pytest.raises(RuntimeError, match="ReadTimeout"):
        preview.run(tmp_path)
    assert preview.run(tmp_path)["done_count"] == 1
    assert preview.requests[0] == preview.requests[1]
    assert preview.state["version"] == "3"


def test_simulation_leaves_an_existing_scene_in_place(tmp_path):
    preview = PreviewDateTest()
    preview.state.update(active=True, version="3")
    with pytest.raises(ValueError, match="已有活动场景"):
        preview.run(tmp_path)
    assert not preview.requests


def test_simulation_checks_no_new_credit_batches(tmp_path):
    preview = PreviewDateTest()
    preview.changed_credit = True
    with pytest.raises(ValueError, match="Credit 批次发生变化"):
        preview.run(tmp_path)


def test_user_only_ci_mode_reuses_one_session_and_writes_a_snapshot(monkeypatch, tmp_path):
    preview = PreviewDateTest()
    preview.run(tmp_path / "setup")
    report = tmp_path / "m06-rewards.json"
    monkeypatch.setenv("BUZZING_ENV", "preview")
    monkeypatch.delenv("BUZZING_REWARDS_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("BUZZING_REWARDS_TOKEN", raising=False)
    monkeypatch.setattr("sys.argv", ["rewards_simulation", "--inspect-user", "--expected-version", "3",
                                    "--report", str(report)])
    logins = []
    requests = []

    def login(account, _):
        logins.append(account)
        return "mock-qa-session"

    def read(request):
        assert request.headers["Authorization"] == "Bearer mock-qa-session"
        requests.append(request.url.path)
        return preview.user(request)

    def client(*, bearer_token):
        return httpx.Client(base_url="https://preview-api.buzzing.app", transport=httpx.MockTransport(read),
                            headers={"Authorization": f"Bearer {bearer_token}"})

    monkeypatch.setattr(rewards_simulation, "get_test_jwt", login)
    monkeypatch.setattr(rewards_simulation, "create_client", client)
    rewards_simulation.main()
    assert logins == ["tester1"]
    assert set(requests) >= {f"{REWARDS}/receipts", HISTORY, "/api/v1/notifications/list"}
    assert all(not path.startswith(ADMIN) for path in requests)
    evidence = report.read_text(encoding="utf-8")
    assert "mock-qa-session" not in evidence
    snapshot = json.loads(evidence)
    assert snapshot["version"] == 3 and snapshot["done_count"] == 1
    assert snapshot["task_receipts"] == []
    assert "no_payout_verified" not in snapshot


def test_user_only_check_rejects_a_different_scene_version(tmp_path):
    preview = PreviewDateTest()
    preview.run(tmp_path)
    with httpx.Client(base_url="https://preview-api.buzzing.app", transport=httpx.MockTransport(preview.user)) as user:
        simulator = FriendSimulation(None, RewardsAPI(user, preview.uid, []), preview.case)
        with pytest.raises(ValueError, match="同一模拟场景版本"):
            simulator.inspect_user(expected_version=4)
