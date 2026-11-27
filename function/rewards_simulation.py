"""Advance M06 friend progress with the Preview Rewards date-test API."""

import argparse
import fcntl
import json
import os
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

import pytest

from function.api_client import create_client, request
from function.identity import get_test_jwt
from function.response_checks import expect_native
from function.rewards_flow import RunJournal
from function.rewards_run import (
    HISTORY, REWARDS, RewardsAPI, manila_day, now, number, one, require, rows,
)
from function.settings import get_environment, load_test_data


ADMIN = "/api/v1/admin/tools/rewards-date-test"
CASE_NAME = "m06_friend_simulation"


def verify_friend_task(task, case):
    config = task["config"]
    require(task.get("online") is True and number(task["version"]) == case["task_version"]
            and config["type"] == "friends" and config["pay_mode"] == "once"
            and number(config["reward_cents"]) == case["amount_cents"] and number(config["friend_limit"]) == 2,
            "好友任务的版本、奖励金额或上限与开发交接不一致")
    conditions = rows(config, "friend_requirements")
    condition_ids = {(row.get("condition") or {}).get("action"): row["id"] for row in conditions}
    require(len(conditions) == len(case["friend_conditions"]) and condition_ids == case["friend_conditions"],
            "好友任务条件与开发交接不一致")
    for row in conditions:
        condition = row["condition"]
        action = condition.get("action")
        require(not row.get("task_id") and condition["measure"] == "count" and number(condition["target"]) == 1
                and (action != "deposit" or condition.get("first_deposit") is True),
                "好友任务应要求一次 App 登录和一次首充")
    unlock = config.get("unlock") or {}
    requirement = unlock.get("condition") or {}
    require(unlock.get("kind") == "action" and requirement.get("action") == "trade"
            and requirement.get("measure") == "count" and number(requirement.get("target")) == 1000,
            "好友任务的邀请人解锁规则与开发交接不一致")


class DateTestSimulation:
    def __init__(self, admin_client, user, case):
        self.admin_client = admin_client
        self.user = user
        self.case = case
        self.path = f"{ADMIN}/{case['uid']}/{case['task_id']}"
        self.user_path = f"{REWARDS}/tasks/{case['task_id']}"

    def admin(self, method, path, **kwargs):
        response = request(self.admin_client, method, path, operation="rewards_date_test", **kwargs)
        body = expect_native(response, status=200, schema={"type": "object"})
        if "code" in body:
            require(type(body["code"]) is int and body["code"] == 0 and isinstance(body.get("data"), dict),
                    "日期推进管理接口返回业务错误")
            return body["data"]
        return body

    def check_scene(self, scene):
        require(number(scene["uid"]) == self.case["uid"] and scene["task_id"] == self.case["task_id"],
                "后台返回了其他 UID 或任务的场景")
        return number(scene["version"])

    def scene(self):
        scene = self.admin("GET", self.path)
        self.check_scene(scene)
        return scene

    def configured_task(self):
        return one((row for row in rows(self.admin("GET", f"{ADMIN}/tasks"), "tasks")
                    if row.get("id") == self.case["task_id"]), "后台未返回指定 Rewards 任务")

    def change(self, journal, phase, version, action, **data):
        saved = journal.load(phase)
        if saved is not None:
            return saved
        key = f"m06-{self.case['run_id']}-{phase}"
        body = {"action": action, "expected_version": str(version), **data}
        intent = journal.load(f"{phase}.intent")
        if intent is None:
            journal.save(f"{phase}.intent", body)
        else:
            require(intent == body, "续跑提交与本轮已记录的模拟变更不一致")
        scene = self.admin("POST", self.path, headers={"Idempotency-Key": key}, json=body)
        require(self.check_scene(scene) == version + 1 and scene.get("active") is (action != "reset"),
                "模拟场景未取得预期的新版本")
        journal.save(phase, scene)
        return scene

    def credit_baseline(self):
        return {
            "receipt_revision": number(self.user.get(f"{REWARDS}/overview")["receipt_revision"]),
            "batch_ids": self.batch_ids(),
            "notice_ids": self.notice_ids(),
        }

    def batch_ids(self):
        return sorted(number(batch["id"]) for batch in self.user.paged(HISTORY, "batches", "cursor", "next_cursor"))

    def notice_ids(self):
        return sorted(str(row["id"]) for row in self.user.notices()
                      if isinstance(row.get("message_params"), dict)
                      and row["message_params"].get("task_id") == self.case["task_id"])

    def verify_no_payout(self, baseline):
        require(not self.task_receipts(baseline["receipt_revision"]), "检测到该任务的新真实回执")
        require(self.batch_ids() == baseline["batch_ids"], "测试账号的 Credit 批次发生变化")
        require(self.notice_ids() == baseline["notice_ids"], "检测到该任务的发奖通知变化")

    def task_receipts(self, after_revision):
        receipts = self.user.paged(f"{REWARDS}/receipts", "receipts", "after_revision", "next_revision", after_revision)
        return [row for row in receipts if row.get("task_id") == self.case["task_id"]]


class FriendSimulation(DateTestSimulation):
    def verify_task(self):
        verify_friend_task(self.configured_task(), self.case)

    def check_friend_conditions(self, friend, done):
        expected = {key: action == "app_login" or done for action, key in self.case["friend_conditions"].items()}
        actual = {row["id"]: row.get("done") is True for row in rows(friend, "progress")}
        require(actual == expected, "模拟好友的条件进度与本轮预期不一致")

    def verify_user_projection(self, *, done, expected_version=None, friend_id=None):
        detail = self.user.get(self.user_path)
        page = self.user.get(f"{self.user_path}/friends", page_size=20)
        versions = []
        for reply in (detail, page):
            context = reply.get("test_context")
            require(isinstance(context, dict), "用户侧未读取到活动的模拟场景")
            versions.append(number(context.get("version")))
        version = versions[0]
        require(version > 0 and versions[1] == version and (expected_version is None or version == expected_version),
                "用户侧未读取到同一模拟场景版本")
        require(page.get("has_more") is not True, "模拟好友列表超出本轮预期数量")
        friend = one(rows(page, "friends"), "用户侧应只返回本轮的一位模拟好友")
        require(isinstance(friend.get("id"), str) and friend["id"].startswith("test:")
                and (friend_id is None or friend["id"] == friend_id), "用户侧未返回本轮模拟好友")
        self.check_friend_conditions(friend, done)
        require(number(page["done_count"]) == int(done), "用户侧达标好友数与模拟条件不一致")
        return {"version": version, "friend_id": friend["id"], "done_count": int(done),
                "task_qualified": detail.get("qualified") is True, "observed_at": now().isoformat()}

    def verify_projection(self, scene, key, done):
        projection = one((row["friend"] for row in rows(scene, "friend_projections") if row.get("key") == key),
                         "后台未返回本轮模拟好友")
        self.check_friend_conditions(projection, done)
        result = self.verify_user_projection(done=done, expected_version=self.check_scene(scene),
                                             friend_id=projection["id"])
        return {**result, "friend_key": key}

    def inspect_user(self, expected_version):
        result = self.verify_user_projection(done=True, expected_version=expected_version)
        receipts = self.task_receipts(0)
        return {
            **result, "account": self.case["account"], "uid": self.case["uid"], "task_id": self.case["task_id"],
            "credit_and_notifications_snapshot": self.credit_baseline(),
            "task_receipts": [{key: row.get(key) for key in ("revision", "unit_id", "batch_id", "amount_cents", "paid_at")}
                              for row in receipts],
        }

    def run(self, journal):
        final = journal.load("result")
        if final is not None:
            return final
        baseline = journal.load("baseline")
        if baseline is None:
            self.verify_task()
            initial = self.scene()
            require(initial.get("active") is False, "此 UID 和任务已有活动场景，可使用 --inspect 查证")
            baseline = {"version": self.check_scene(initial), **self.credit_baseline()}
            journal.save("baseline", baseline)

        created = self.change(journal, "create", baseline["version"], "create",
                              simulate_unlock=self.case["simulate_unlock"])
        date = manila_day(created["now"])
        key = f"m06-{self.case['run_id']}"

        def set_progress(phase, version, done):
            conditions = [{"id": key, "completed": action == "app_login" or done}
                          for action, key in self.case["friend_conditions"].items()]
            return self.change(journal, phase, version, "set_friends", friends=[
                {"key": key, "invited_on": date, "days": [{"date": date, "conditions": conditions}]},
            ])

        partial = set_progress("partial", self.check_scene(created), False)
        if journal.load("partial_check") is None:
            journal.save("partial_check", self.verify_projection(partial, key, False))
        complete = set_progress("complete", self.check_scene(partial), True)
        result = self.verify_projection(complete, key, True)
        self.verify_no_payout(baseline)
        result["no_payout_verified"] = True
        journal.save("result", result)
        return result

    def inspect(self):
        scene = self.scene()
        require(scene.get("active") is True, "指定 UID 和任务当前没有活动场景")
        entry = one(rows(scene, "friend_projections"), "此用例应只有一个模拟好友")
        checks = {row["id"]: row.get("done") is True for row in rows(entry["friend"], "progress")}
        return self.verify_projection(scene, entry["key"], checks[self.case["friend_conditions"]["deposit"]])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--run-id", type=UUID)
    action.add_argument("--inspect", action="store_true")
    action.add_argument("--inspect-user", action="store_true")
    parser.add_argument("--expected-version", type=int, help="Expected Preview scene version for --inspect-user")
    parser.add_argument("--report", type=Path, help="Write a new JSON evidence file")
    parser.add_argument("--journal", type=Path, default=Path(".local/rewards/simulation"))
    args = parser.parse_args()
    try:
        require(get_environment() == "preview", "Rewards 模拟仅支持 Preview")
        require(args.expected_version is None or (args.inspect_user and args.expected_version > 0),
                "--expected-version 仅用于 --inspect-user，须为正整数")
        data = load_test_data()
        case = dict(data["rewards_cases"][CASE_NAME])
        admin_token = os.getenv("BUZZING_REWARDS_ADMIN_TOKEN", "").strip().removeprefix("Bearer ")
        require(args.inspect_user or admin_token, "缺少 BUZZING_REWARDS_ADMIN_TOKEN")
        user_token = os.getenv("BUZZING_REWARDS_TOKEN") or get_test_jwt(case["account"], data[case["account"]])
        with create_client(bearer_token=user_token) as client:
            user = RewardsAPI(client, case["uid"], [])
            if args.inspect_user:
                result = FriendSimulation(None, user, case).inspect_user(args.expected_version)
            else:
                with create_client(bearer_token=admin_token) as admin:
                    simulation = FriendSimulation(admin, user, case)
                    if args.inspect:
                        result = simulation.inspect()
                    else:
                        result = run_new_scene(simulation, args.journal, args.run_id)
        if args.report is not None:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            with args.report.open("x", encoding="utf-8") as stream:
                json.dump(result, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
    except (ValueError, RuntimeError, OSError, KeyError, TypeError, pytest.fail.Exception) as error:
        detail = str(error) if isinstance(error, (ValueError, RuntimeError, pytest.fail.Exception)) else type(error).__name__
        raise SystemExit(f"M06 模拟未完成，{detail}") from None
    print(f"M06 模拟场景版本 {result['version']}，达标好友 {result['done_count']} 位")


def run_new_scene(simulation, root, run_id):
    with simulation_journal(simulation.case, root, run_id) as journal:
        return simulation.run(journal)


@contextmanager
def simulation_journal(case, root, run_id):
    case["run_id"] = str(run_id)
    journal = RunJournal(root, case)
    with (root / f"{case['uid']}-{case['task_id']}.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        frozen = journal.load("case")
        if frozen is None:
            journal.save("case", case)
        else:
            require(frozen == case, "现有运行记录关联另一份用例配置")
        yield journal


if __name__ == "__main__":
    main()
