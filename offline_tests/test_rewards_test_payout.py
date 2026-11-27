import json
import sys
from copy import deepcopy
from datetime import datetime, timezone
from shutil import copytree

import httpx
import pytest

from function import rewards_test_payout
from function.rewards_flow import RunJournal
from function.rewards_run import HISTORY, REWARDS, RewardsAPI, RewardsPending
from function.rewards_test_payout import CASES, GRANT_LOGS, DateTestPayout, compare_rounds
from function.settings import load_test_data


@pytest.fixture(autouse=True)
def test_date_clock(monkeypatch):
    monkeypatch.setattr("function.rewards_test_payout.now", lambda: datetime(2026, 11, 27, 7, tzinfo=timezone.utc))


class TestCreditPreview:
    __test__ = False

    def __init__(self, kind):
        self.case = {**load_test_data()["rewards_cases"][CASES[kind]],
                     "run_id": "8307eff7-6396-42b9-bbd6-b12b8c82d49b"}
        self.kind = kind
        self.day = "2026-11-27"
        self.amount = self.case["amount_cents"]
        self.uid = self.case["uid"]
        self.task_id = self.case["task_id"]
        self.unit_id = "friend:194760148783115440" if kind == "friends" else f"day:{self.day}"
        self.batches = []
        self.scene = {"uid": str(self.uid), "task_id": self.task_id, "version": "0", "active": False,
                      "now": f"{self.day}T04:00:00Z", "projection": {"rewards": []}, "friend_projections": []}
        self.actions = []
        self.committed = {}
        self.hide_notice = False
        self.unavailable_credit = False
        self.extra_unit = False
        self.lose_first_grant_reply = False

    def unit(self):
        return {"id": self.unit_id, "state": "qualified", "amount_cents": str(self.amount),
                "qualifying_day": self.day if self.kind == "daily" else ""}

    def request(self, req):
        path = req.url.path
        if path.endswith("rewards-date-test/tasks"):
            return httpx.Response(200, json={"tasks": [{"id": self.task_id, "online": True, "version": "2", "config": {
                "type": self.kind, "pay_mode": "once" if self.kind == "friends" else "each",
                "reward_cents": str(self.amount), "challenge": {"day_rewards_cents": [str(self.amount), "300"]},
                "friend_limit": 2,
                "unlock": {"kind": "action", "condition": {"action": "trade", "measure": "count", "target": "1000"}},
                "friend_requirements": [{"id": key, "condition": {"action": action, "measure": "count", "target": "1",
                                                                  "first_deposit": action == "deposit"}}
                                        for action, key in self.case.get("friend_conditions", {}).items()],
            }}]})
        if path == GRANT_LOGS:
            assert req.url.params["source_type"] == "task"
            assert req.url.params["user_id"] == str(self.uid)
            return httpx.Response(200, json={"items": [self.grant_entry(batch["id"]) for batch in self.batches],
                                             "has_more": False})
        assert path.endswith(f"/{self.uid}/{self.task_id}")
        if req.method == "GET":
            return httpx.Response(200, json=self.scene)
        body = json.loads(req.content)
        action = body["action"]
        key = req.headers["Idempotency-Key"]
        self.actions.append((action, key))
        if key in self.committed:
            saved_body, scene = self.committed[key]
            assert saved_body == body
            return httpx.Response(200, json=scene)
        assert body["expected_version"] == self.scene["version"]
        self.scene["version"] = str(int(self.scene["version"]) + 1)
        if action == "create":
            assert body["simulate_unlock"] is True
            self.scene.update(active=True, now=f"{self.day}T04:00:00Z",
                              projection={"rewards": []}, friend_projections=[])
        elif action == "set_day":
            assert body["date"] == self.day and body["counted"] is True
            self.scene["projection"]["rewards"] = [self.unit()]
        elif action == "advance":
            self.scene["now"] = "2026-11-28T04:00:00Z"
        elif action == "set_friends":
            friend = body["friends"][0]
            progress = [{"id": item["id"], "done": item["completed"]} for item in friend["days"][0]["conditions"]]
            self.scene["friend_projections"] = [{"key": friend["key"], "friend": {
                "user_id": self.unit_id.removeprefix("friend:"), "progress": progress,
            }}]
            self.scene["projection"]["rewards"] = [self.unit()]
        elif action == "grant_rewards":
            unit = self.scene["projection"]["rewards"][0]
            if unit["state"] != "paid":
                batch_id = str(71 + len(self.batches))
                unit.update(state="paid", batch_id=batch_id, paid_at="2026-11-27T08:00:00Z")
                self.batches.append({"id": batch_id, "granted": self.amount * 10_000, "status": "active",
                                     "available": 0 if self.unavailable_credit else self.amount * 10_000,
                                     "name": "[TEST] Example"})
        elif action == "reset":
            self.scene["active"] = False
        else:
            raise AssertionError(action)
        if self.extra_unit and action in {"advance", "set_friends"}:
            self.scene["projection"]["rewards"].append({**self.unit(), "id": "extra"})
        self.committed[key] = body, deepcopy(self.scene)
        if action == "grant_rewards" and self.lose_first_grant_reply:
            self.lose_first_grant_reply = False
            raise httpx.ReadTimeout("lost committed grant reply")
        return httpx.Response(200, json=self.scene)

    def grant_entry(self, batch_id):
        return {"id": f"grant-{batch_id}", "batch_id": batch_id, "source_type": "task", "user_id": str(self.uid),
                "source_ref": f"date-test:{self.task_id}", "amount": self.amount * 10_000, "name": "[TEST] Example"}

    def user(self, req):
        assert req.method == "GET"
        path = req.url.path
        if path == "/api/v1/auth/session":
            return httpx.Response(200, json={"auth_level": 1, "user_id": str(self.uid)})
        assert path not in {f"{REWARDS}/overview", f"{REWARDS}/receipts"}, "Date-tool payout uses batch evidence"
        if path == HISTORY:
            return httpx.Response(200, json={"batches": self.batches, "has_more": False})
        if path == "/api/v1/notifications/list":
            notices = [] if self.hide_notice else [
                {"id": f"notice-{batch['id']}", "type": "credit_manual_received", "related_type": "credit_batch",
                 "related_id": batch["id"], "action_type": "view_credit",
                 "message_params": {"batch_id": batch["id"], "amount": batch["granted"]}}
                for batch in self.batches
            ]
            return httpx.Response(200, json={"notifications": notices, "total": len(notices), "page": 1, "page_size": 50})
        assert path == f"{REWARDS}/tasks/{self.task_id}"
        return httpx.Response(200, json={**self.scene["projection"], "test_context": {"version": self.scene["version"]}})

    def run(self, root, *, grant, reset=False):
        with (httpx.Client(base_url="https://preview-api.buzzing.app", transport=httpx.MockTransport(self.request)) as admin,
              httpx.Client(base_url="https://preview-api.buzzing.app", transport=httpx.MockTransport(self.user)) as user):
            simulation = DateTestPayout(admin, RewardsAPI(user, self.uid, []), self.case)
            journal = RunJournal(root, self.case)
            return simulation.reset_completed(journal) if reset else simulation.run(journal, grant=grant, wait_seconds=0)


@pytest.mark.parametrize(("kind", "steps"), [("daily", ["set_day", "advance"]), ("friends", ["set_friends"])])
def test_date_tool_grant_reconciles_generic_notice_and_repeats_same_unit(kind, steps, tmp_path):
    preview = TestCreditPreview(kind)
    assert preview.run(tmp_path, grant=False)["status"] == "prepared"
    assert [action for action, _ in preview.actions] == ["create", *steps]
    assert not preview.batches
    result = preview.run(tmp_path, grant=True)
    assert result["batch_id"] == 71 and result["repeat_verified"]
    assert result["amount_raw"] == preview.amount * 10_000
    assert result["available_raw"] == result["amount_raw"]
    assert [action for action, _ in preview.actions] == ["create", *steps, "grant_rewards", "grant_rewards"]
    assert preview.actions[-2][1] != preview.actions[-1][1]
    assert len(preview.batches) == 1


def test_grant_timeout_and_delayed_notice_resume_without_extra_credit(tmp_path):
    preview = TestCreditPreview("friends")
    preview.lose_first_grant_reply = True
    with pytest.raises(RuntimeError, match="ReadTimeout"):
        preview.run(tmp_path, grant=True)
    preview.hide_notice = True
    with pytest.raises(RewardsPending, match="Inbox"):
        preview.run(tmp_path, grant=True)
    assert preview.actions[-1] == preview.actions[-2]
    preview.hide_notice = False
    assert preview.run(tmp_path, grant=True)["repeat_verified"]
    assert len(preview.batches) == 1


def test_ci_resume_uses_the_same_journal_after_a_committed_grant_timeout(monkeypatch, tmp_path):
    preview = TestCreditPreview("daily")
    prepared, grant, retry = [tmp_path / name for name in ("prepared", "grant", "retry")]
    RunJournal(prepared, preview.case).save("case", preview.case)
    assert preview.run(prepared, grant=False)["status"] == "prepared"

    def client(*, bearer_token):
        handler = preview.request if bearer_token == "qa-admin" else preview.user
        return httpx.Client(base_url="https://preview-api.buzzing.app", transport=httpx.MockTransport(handler))

    monkeypatch.setattr(rewards_test_payout, "create_client", client)
    monkeypatch.setenv("BUZZING_ENV", "preview")
    monkeypatch.setenv("BUZZING_REWARDS_ADMIN_TOKEN", "qa-admin")
    monkeypatch.setenv("BUZZING_REWARDS_TOKEN", "qa-user")
    copytree(prepared, grant)
    preview.lose_first_grant_reply = True
    monkeypatch.setattr(sys, "argv", ["rewards_test_payout", "--resume", "--grant", "--journal", str(grant)])
    with pytest.raises(SystemExit, match="ReadTimeout"):
        rewards_test_payout.main()
    copytree(grant, retry)
    monkeypatch.setattr(sys, "argv", ["rewards_test_payout", "--resume", "--grant", "--journal", str(retry)])
    rewards_test_payout.main()

    result = RunJournal(retry, preview.case).load("result")
    assert result["status"] == "passed" and result["run_id"] == preview.case["run_id"]
    assert [action for action, _ in preview.actions] == [
        "create", "set_day", "advance", "grant_rewards", "grant_rewards", "grant_rewards",
    ]
    assert preview.actions[3] == preview.actions[4]
    assert len(preview.batches) == 1


def test_ci_resume_rejects_a_journal_from_a_different_configuration(tmp_path):
    preview = TestCreditPreview("daily")
    frozen = {**preview.case, "amount_cents": preview.amount + 1}
    RunJournal(tmp_path, frozen).save("case", frozen)
    with pytest.raises(ValueError, match="当前 ci.yaml 不一致"):
        rewards_test_payout.resume_case(tmp_path, load_test_data()["rewards_cases"])


def test_grant_never_submits_more_than_one_qualified_unit(tmp_path):
    preview = TestCreditPreview("friends")
    preview.extra_unit = True
    with pytest.raises(ValueError, match="只有一个可发奖单元"):
        preview.run(tmp_path, grant=True)
    assert all(action != "grant_rewards" for action, _ in preview.actions)


def test_granted_credit_has_to_be_available(tmp_path):
    preview = TestCreditPreview("friends")
    preview.unavailable_credit = True
    with pytest.raises(ValueError, match="可用金额"):
        preview.run(tmp_path, grant=True)
    assert [action for action, _ in preview.actions].count("grant_rewards") == 1


def test_existing_scene_is_only_adopted_at_the_explicit_version(tmp_path):
    preview = TestCreditPreview("friends")
    preview.run(tmp_path / "first", grant=False)
    version = int(preview.scene["version"])
    preview.case["run_id"] = "b2dd07cc-eaf1-43cf-aec3-681d123a1a62"
    preview.case["scene_version"] = version + 1
    with pytest.raises(ValueError, match="不是指定版本"):
        preview.run(tmp_path / "adopted", grant=True)
    preview.case["scene_version"] = version
    result = preview.run(tmp_path / "adopted", grant=True)
    assert result["scene_start_version"] == version
    assert [action for action, _ in preview.actions] == ["create", "set_friends", "grant_rewards", "grant_rewards"]


def test_two_rounds_use_new_scene_and_reset_retains_the_paid_batch(tmp_path):
    preview = TestCreditPreview("friends")
    first = preview.run(tmp_path, grant=True)
    first_path = tmp_path / preview.case["run_id"] / "result.json"
    assert preview.run(tmp_path, grant=False, reset=True)["retained_batch_id"] == 71
    preview.case["run_id"] = "b2dd07cc-eaf1-43cf-aec3-681d123a1a62"
    second = preview.run(tmp_path, grant=True)
    second_path = tmp_path / preview.case["run_id"] / "result.json"
    assert first["friend_key"] != second["friend_key"]
    assert compare_rounds([first_path, second_path])["batch_ids"] == [71, 72]
    with pytest.raises(ValueError, match="两轮必须使用独立"):
        compare_rounds([first_path, first_path])
