"""Prepare and reconcile explicit Preview Rewards date-tool Credit grants."""

import argparse
import json
import os
import time
from datetime import date, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from function.api_client import create_client
from function.identity import get_test_jwt
from function.rewards_run import (
    FIXTURE_MARKERS, HISTORY, MAX_PAGES, RewardsAPI, RewardsPending, manila_day, now,
    number, one, one_published, read_json, require, rows, utc,
)
from function.rewards_simulation import CASE_NAME, DateTestSimulation, simulation_journal, verify_friend_task
from function.settings import get_environment, load_test_data


CASES = {"daily": "m06_date_daily_grant", "friends": CASE_NAME}
GRANT_LOGS = "/api/v1/admin/credit/grant-logs"


def eligible_units(scene):
    projection = scene["projection"]
    require(not projection.get("blocked_reasons") and projection.get("status") != "locked",
            "模拟任务仍有阻塞条件")
    return [unit for unit in rows(projection, "rewards") if unit.get("state") == "qualified"
            and number(unit["amount_cents"]) > 0 and not unit.get("blocked_reasons")]


def single_new_unit(scene, amount_cents):
    require(all(row.get("state") not in {"paid", "reconciling"} for row in rows(scene["projection"], "rewards")),
            "场景存在已付或待对账奖励")
    unit = one(eligible_units(scene), "发奖前必须只有一个可发奖单元")
    require(number(unit["amount_cents"]) == amount_cents, "发奖单元金额与本轮上限不同")
    return unit


class DateTestPayout(DateTestSimulation):
    def verify_task(self):
        task = self.configured_task()
        config = task["config"]
        case = self.case
        require(task.get("online") is True and number(task["version"]) == case["task_version"]
                and config["type"] == case["scenario"], "目标任务未上线、版本变化或类型不符")
        serialized = json.dumps(config, ensure_ascii=False).lower()
        require(not any(marker in serialized for marker in FIXTURE_MARKERS), "目标任务仅供界面展示")
        if case["scenario"] == "daily":
            challenge = config["challenge"]
            amounts = challenge["day_rewards_cents"]
            require(config["pay_mode"] == "each" and amounts and number(amounts[0]) == case["amount_cents"]
                    and not challenge.get("base_condition"), "每日任务首个奖励单元或达标规则变化")
        else:
            verify_friend_task(task, case)

    def prepared_scene(self, journal, baseline):
        if "adopted_scene" in baseline:
            return baseline["adopted_scene"]

        created = self.change(journal, "create", baseline["version"], "create",
                              simulate_unlock=self.case["simulate_unlock"])
        first_day = manila_day(created["now"])
        if self.case["scenario"] == "friends":
            conditions = [{"id": key, "completed": True} for key in self.case["friend_conditions"].values()]
            return self.change(journal, "friend", self.check_scene(created), "set_friends", friends=[
                {"key": f"m06-{self.case['run_id']}", "invited_on": first_day,
                 "days": [{"date": first_day, "conditions": conditions}]},
            ])

        first = self.change(journal, "first_day", self.check_scene(created), "set_day",
                            date=first_day, counted=True)
        advanced = self.change(journal, "advance", self.check_scene(first), "advance")
        require(date.fromisoformat(manila_day(advanced["now"])) == date.fromisoformat(first_day) + timedelta(days=1),
                "服务端没有推进到下一个 Manila 模拟日期")
        require(one(eligible_units(advanced), "每日模拟只应产生一个可发奖单元").get("qualifying_day") == first_day,
                "模拟奖励所属日期与首日不一致")
        return advanced

    def prepare(self, journal):
        baseline = journal.load("baseline")
        if baseline is None:
            self.verify_task()
            current = self.scene()
            version = self.check_scene(current)
            adopt = self.case.get("scene_version")
            if adopt is None:
                require(current.get("active") is False, "当前已有活动场景，须显式选择已核对的场景版本")
            else:
                require(current.get("active") is True and version == adopt, "活动场景不是指定版本")
            baseline = {"batch_ids": self.batch_ids(), "version": version, "observed_at": now().isoformat()}
            if adopt is not None:
                baseline["adopted_scene"] = current
            journal.save("baseline", baseline)
        prepared = self.prepared_scene(journal, baseline)
        amount = self.case["amount_cents"]
        unit = single_new_unit(prepared, amount)
        info = {"version": self.check_scene(prepared), "unit_id": unit["id"], "amount_cents": amount}
        if self.case["scenario"] == "friends":
            entry = one(rows(prepared, "friend_projections"), "好友发奖前应只有一个模拟好友")
            friend = entry["friend"]
            conditions = {row["id"] for row in rows(friend, "progress") if row.get("done") is True}
            require(conditions == set(self.case["friend_conditions"].values())
                    and unit["id"] == f"friend:{friend['user_id']}",
                    "可发奖单元与本轮达标好友不一致")
            info["friend_key"] = entry["key"]
        return baseline, info

    def grant_log(self, batch_id, since):
        cursor, seen, matches = "", set(), []
        for _ in range(MAX_PAGES):
            page = self.admin("GET", GRANT_LOGS, params={
                "user_id": self.case["uid"], "source_type": "task", "name": "[TEST]",
                "granted_from": int(utc(since).timestamp()), "page_size": 50, "cursor": cursor,
            })
            matches.extend(row for row in rows(page, "items") if number(row["batch_id"]) == batch_id)
            if page.get("has_more") is False:
                return one_published(matches, "后台还未出现唯一测试 Credit 发放记录")
            cursor = page.get("next_cursor")
            require(isinstance(cursor, str) and cursor and cursor not in seen, "后台发放记录分页游标不推进")
            seen.add(cursor)
        raise ValueError("后台发放记录超过分页上限")

    def reconcile(self, command_reply, baseline, prepared):
        scene = self.scene()
        version = self.check_scene(command_reply)
        require(self.check_scene(scene) == version and scene.get("active") is True, "发奖后模拟场景已被改动")
        unit = one_published((row for row in rows(scene["projection"], "rewards")
                              if row["id"] == prepared["unit_id"] and row["state"] == "paid"),
                             "场景尚未读到已付测试奖励")
        batch_id = number(unit["batch_id"])
        require(batch_id > 0 and batch_id not in baseline["batch_ids"]
                and number(unit["amount_cents"]) == prepared["amount_cents"], "测试批次不是本轮新增的预期金额")
        require(utc(unit["paid_at"]) >= utc(baseline["observed_at"]).replace(microsecond=0),
                "测试奖励发放时间早于本轮基线")
        user_detail = self.user.get(self.user_path)
        context = user_detail.get("test_context") or {}
        if number(context.get("version", 0)) != version:
            raise RewardsPending("用户任务投影尚未更新至发奖版本")
        user_unit = one_published((row for row in rows(user_detail, "rewards")
                                   if row["id"] == unit["id"] and row["state"] == "paid"),
                                  "用户任务投影尚未读到已付单元")
        require(number(user_unit["batch_id"]) == batch_id and number(user_unit["amount_cents"]) == prepared["amount_cents"],
                "用户和后台任务投影批次或金额不一致")

        batches = self.user.paged(HISTORY, "batches", "cursor", "next_cursor")
        batch = one_published((row for row in batches if number(row["id"]) == batch_id), "用户 Credit 历史暂无本轮批次")
        require(sorted(number(row["id"]) for row in batches) == sorted([*baseline["batch_ids"], batch_id]),
                "本轮 Credit 历史新增记录超过预期")
        raw = prepared["amount_cents"] * 10_000
        require(number(batch["granted"]) == raw and number(batch["available"]) == raw
                and batch["status"] == "active" and batch["name"].startswith("[TEST]"),
                "用户 Credit 发放金额、可用金额或批次状态不符合测试发奖")
        log = self.grant_log(batch_id, baseline["observed_at"])
        require(number(log["user_id"]) == self.case["uid"] and number(log["amount"]) == raw
                and log["source_type"] == "task" and log["source_ref"] == f"date-test:{self.case['task_id']}"
                and log["name"] == batch["name"], "后台发放记录的来源、归属或金额不一致")
        notice = one_published((row for row in self.user.notices()
                                if row.get("related_type") == "credit_batch" and number(row["related_id"]) == batch_id),
                               "Inbox 尚未出现唯一测试 Credit 通知")
        params = notice["message_params"]
        require(notice["type"] in {"credit_granted", "credit_manual_received"} and notice["action_type"] == "view_credit"
                and number(params["batch_id"]) == batch_id and number(params["amount"]) == raw,
                "通用到账通知的批次、金额或动作不一致")
        return {"batch_id": batch_id, "paid_at": unit["paid_at"], "grant_log_id": log["id"],
                "notification_id": notice["id"], "source_ref": log["source_ref"], "amount_raw": raw,
                "available_raw": number(batch["available"])}

    def wait_reconcile(self, reply, baseline, prepared, duration):
        deadline = time.monotonic() + duration
        while True:
            try:
                return self.reconcile(reply, baseline, prepared)
            except RewardsPending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise
                time.sleep(min(remaining, 3))

    def run(self, journal, *, grant, wait_seconds):
        result = journal.load("result")
        if result is not None:
            return result
        baseline, prepared = self.prepare(journal)
        if journal.load("grant.intent") is None:
            current = self.scene()
            require(self.check_scene(current) == prepared["version"]
                    and single_new_unit(current, prepared["amount_cents"])["id"] == prepared["unit_id"],
                    "发奖前的可发奖单元或场景已经变化")
            require(self.batch_ids() == baseline["batch_ids"], "模拟推进期间 Credit 批次发生变化")
        if not grant:
            require(journal.load("grant.intent") is None, "此轮已经提交发奖，请用原运行 ID 继续对账")
            return {"status": "prepared", **prepared}

        granted = self.change(journal, "grant", prepared["version"], "grant_rewards")
        first = journal.load("settled")
        if first is None:
            first = self.wait_reconcile(granted, baseline, prepared, wait_seconds)
            journal.save("settled", first)
        if journal.load("repeat.intent") is None:
            current = self.scene()
            require(self.check_scene(current) == self.check_scene(granted) and not eligible_units(current)
                    and all(row.get("state") != "reconciling" for row in rows(current["projection"], "rewards")),
                    "同一场景已有新发奖候选，暂停重复提交")
        repeated = self.change(journal, "repeat", self.check_scene(granted), "grant_rewards")
        require(self.wait_reconcile(repeated, baseline, prepared, wait_seconds) == first,
                "同一单元重复提交后，发放记录、批次或通知发生变化")
        result = {"status": "passed", "run_id": self.case["run_id"], "uid": self.case["uid"],
                  "task_id": self.case["task_id"], "task_version": self.case["task_version"],
                  "scenario": self.case["scenario"], "scene_start_version": baseline["version"],
                  **prepared, **first, "final_scene_version": self.check_scene(repeated), "repeat_verified": True}
        journal.save("result", result)
        return result

    def reset_completed(self, journal):
        result = journal.load("result")
        require(result is not None and result.get("repeat_verified") is True, "仅可重置本轮已完成对账的场景")
        closed = self.change(journal, "reset", result["final_scene_version"], "reset")
        require(result["batch_id"] in self.batch_ids(), "重置后未找到已到账的 Credit 批次")
        return {"status": "reset", "version": self.check_scene(closed), "retained_batch_id": result["batch_id"]}


def compare_rounds(paths):
    first, second = [read_json(path) for path in paths]
    require(all(row.get("status") == "passed" and row.get("repeat_verified") is True for row in (first, second)),
            "两轮均须完成发放、到账和重放检查")
    require(all(first[key] == second[key] for key in ("task_id", "task_version", "scenario", "amount_cents")),
            "两轮需要相同任务版本和发放口径")
    require(first["run_id"] != second["run_id"] and first["batch_id"] != second["batch_id"]
            and (first["uid"], first["scene_start_version"]) != (second["uid"], second["scene_start_version"]),
            "两轮必须使用独立场景及不同到账批次")
    if first["scenario"] == "friends":
        require(first["friend_key"] != second["friend_key"], "好友复跑必须使用不同模拟好友")
    return {"status": "two_rounds_checked", "scenario": first["scenario"], "batch_ids": [first["batch_id"], second["batch_id"]]}


def resume_case(root, configured_cases):
    path = one(root.glob("*/case.json"), "续跑产物必须包含且仅包含一轮 M06 用例记录")
    frozen = read_json(path)
    run_id = UUID(frozen["run_id"])
    require(path.parent.name == str(run_id), "续跑目录与原运行 ID 不一致")
    case = dict(configured_cases[CASES[frozen["scenario"]]])
    if "scene_version" in frozen:
        version = frozen["scene_version"]
        require(type(version) is int and version > 0, "原运行记录的场景版本无效")
        case["scene_version"] = version
    require(frozen == {**case, "run_id": str(run_id)}, "续跑产物的用例配置和当前 ci.yaml 不一致")
    return run_id, case


def run_date_test(operation, *, root, case_name=None, run_id=None, scene_version=None, resume=False, wait_seconds=90):
    require(get_environment() == "preview", "测试 Credit 发放仅支持 Preview")
    require(operation in {"prepare", "grant", "reset"}, "未知的 Rewards 日期工具操作")
    require(0 <= wait_seconds <= 600, "等待秒数须为 0 至 600")
    data = load_test_data()
    if resume:
        require(case_name is None and run_id is None and scene_version is None,
                "续跑使用产物中的用例、运行 ID 和场景版本")
        run_id, case = resume_case(root, data["rewards_cases"])
    else:
        require(case_name in CASES and run_id is not None, "须指定日期工具用例和运行 ID")
        case = dict(data["rewards_cases"][CASES[case_name]])
        if scene_version is not None:
            require(scene_version > 0, "已有场景版本须为正整数")
            case["scene_version"] = scene_version
    admin_token = os.getenv("BUZZING_REWARDS_ADMIN_TOKEN", "").strip().removeprefix("Bearer ")
    require(admin_token, "缺少 Preview 后台凭证 BUZZING_REWARDS_ADMIN_TOKEN")
    user_token = os.getenv("BUZZING_REWARDS_TOKEN") or get_test_jwt(case["account"], data[case["account"]])
    with (create_client(bearer_token=admin_token) as admin,
          create_client(bearer_token=user_token) as client,
          simulation_journal(case, root, run_id) as journal):
        runner = DateTestPayout(admin, RewardsAPI(client, case["uid"], []), case)
        if operation == "reset":
            return runner.reset_completed(journal)
        return runner.run(journal, grant=operation == "grant", wait_seconds=wait_seconds)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--case", choices=CASES)
    selector.add_argument("--compare-rounds", type=Path, nargs=2)
    selector.add_argument("--resume", action="store_true", help="Load the original case from the journal directory")
    parser.add_argument("--run-id", type=UUID)
    parser.add_argument("--scene-version", type=int, help="Explicitly use an existing qualified scene")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--grant", action="store_true")
    mode.add_argument("--reset-completed", action="store_true")
    parser.add_argument("--wait-seconds", type=int, default=90)
    parser.add_argument("--journal", type=Path, default=Path(".local/rewards/simulation"))
    args = parser.parse_args()
    try:
        if args.compare_rounds:
            result = compare_rounds(args.compare_rounds)
        else:
            operation = "reset" if args.reset_completed else "grant" if args.grant else "prepare"
            result = run_date_test(operation, root=args.journal, case_name=args.case, run_id=args.run_id,
                                   scene_version=args.scene_version, resume=args.resume, wait_seconds=args.wait_seconds)
    except (ValueError, RuntimeError, OSError, KeyError, TypeError, pytest.fail.Exception) as error:
        message = str(error) if isinstance(error, (ValueError, RuntimeError, pytest.fail.Exception)) else type(error).__name__
        raise SystemExit(f"M06 日期工具发奖未完成，{message}") from None
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
