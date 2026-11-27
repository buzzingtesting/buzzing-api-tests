"""Run M06 Preview business writes and reconcile their Rewards effects."""

import argparse
import fcntl
import json
import os
import re
import time
from contextlib import ExitStack
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import uuid4

import pytest

from function.api_client import create_client
from function.identity import get_privy_email_identity_token, login_with_privy_identity_token
from function.rewards_run import (
    APP_LOGIN, MANILA, REWARDS, RewardsAPI, RewardsPending, check_task, checkpoint, manila_day,
    now, number, parse_case, read_event, read_json, require, utc,
)
from function.settings import get_environment, load_test_data


REFERRAL = "/api/v1/referral/v2"
APP_PLATFORM = "ios"


class RunJournal:
    def __init__(self, root, case):
        self.directory = root / case["run_id"]
        self.directory.mkdir(parents=True, exist_ok=True)
        self.case = case

    def load(self, name):
        path = self.directory / f"{name}.json"
        return read_json(path) if path.exists() else None

    def save(self, name, data):
        path = self.directory / f"{name}.json"
        require(not path.exists(), f"{name} 记录已存在")
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.directory, delete=False) as pending:
            temp = Path(pending.name)
            try:
                json.dump(data, pending, ensure_ascii=False, indent=2)
                pending.write("\n")
                pending.flush()
                os.fsync(pending.fileno())
                os.link(temp, path)
            finally:
                temp.unlink(missing_ok=True)


class Credentials:
    def __init__(self, case, audit, stack):
        self.case = case
        self.audit = audit
        self.stack = stack
        self.identity_tokens = {}

    def identity(self, role):
        if role not in self.identity_tokens:
            prefix = "BUZZING_REWARDS" if role == "beneficiary" else "BUZZING_REWARDS_FRIEND"
            token = os.getenv(f"{prefix}_PRIVY_IDENTITY_TOKEN")
            if not token:
                account = self.case.get(f"{role}_account")
                require(account, f"请为 {role} 配置 ci.yaml 专用账号或 {prefix}_PRIVY_IDENTITY_TOKEN")
                profile = load_test_data().get(account)
                require(isinstance(profile, dict), "ci.yaml 找不到指定的 Rewards 账号")
                token = get_privy_email_identity_token(profile)
            self.identity_tokens[role] = token
        return self.identity_tokens[role]

    def connect(self, bearer, uid):
        client = self.stack.enter_context(create_client(bearer_token=bearer))
        return RewardsAPI(client, uid, self.audit)

    def session(self, role, uid):
        key = "BUZZING_REWARDS_TOKEN" if role == "beneficiary" else "BUZZING_REWARDS_FRIEND_TOKEN"
        bearer = os.getenv(key)
        if not bearer:
            # The ordinary login bootstraps a session without emitting an App Rewards event.
            with create_client() as client:
                result = login_with_privy_identity_token(client, self.identity(role),
                                                         expected_uid=uid, audit=self.audit)
            bearer = result["token"]
        return self.connect(bearer, uid)


def ensure_window(case):
    require(utc(case["starts_at"]) <= now() < utc(case["ends_at"]), "当前时间不在本轮 Preview 窗口")


def login_event(login, intent, uid):
    occurred_at = datetime.fromtimestamp(number(login["auth_at"]), timezone.utc)
    earliest = utc(intent["started_at"]).replace(microsecond=0)
    require(earliest <= occurred_at <= now(), "App 登录服务端 auth_at 不在本次请求窗口")
    if intent["manila_day"]:
        require(occurred_at.astimezone(MANILA).date().isoformat() == intent["manila_day"],
                "App 登录写入跨越了已计划的 Manila 日期")
    return {"type": APP_LOGIN, "uid": uid, "occurred_at": occurred_at.isoformat(),
            "platform": APP_PLATFORM, "action": intent["action"]}


class RewardsFlow:
    def __init__(self, journal, api, invitee, credentials, wait_seconds):
        self.journal = journal
        self.case = journal.case
        self.api = api
        self.invitee = invitee
        self.credentials = credentials
        self.wait_seconds = wait_seconds

    def observe(self, phase, baseline=None, previous=None, event=None):
        saved = self.journal.load(phase)
        if saved is not None:
            return saved
        deadline = time.monotonic() + self.wait_seconds
        while True:
            try:
                result = checkpoint(self.api, self.invitee, self.case, phase, baseline or {},
                                    previous or {}, event)
            except RewardsPending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise
                time.sleep(min(3, remaining))
            else:
                result.update(observed_at=now().isoformat(), event=event)
                self.journal.save(phase, result)
                return result

    def ready_to_write(self):
        ensure_window(self.case)
        overview = self.api.get(f"{REWARDS}/overview")
        require(overview.get("payouts_enabled") is True, "Preview Rewards 未开启真实发奖")
        check_task(self.api.get(f"{REWARDS}/tasks/{self.case['task_id']}"), self.case)
        ensure_window(self.case)

    def app_login(self, action, role, uid, expected_day=None):
        confirmed = self.journal.load(action)
        if confirmed is not None:
            return confirmed
        if self.journal.load(f"{action}.intent"):
            raise ValueError(f"{action} 的请求结果未确认，请凭请求记录和服务端日志查证后处理")
        token = self.credentials.identity(role)
        self.ready_to_write()
        if expected_day is not None:
            require(now().astimezone(MANILA).date() == expected_day, f"登录须在 Manila {expected_day} 执行")
        intent = {"action": action, "uid": uid, "started_at": now().isoformat(),
                  "manila_day": expected_day.isoformat() if expected_day else None}
        self.journal.save(f"{action}.intent", intent)
        # Identity enforces expect_user_id only when the caller already authenticates as that UID.
        client = (self.api if role == "beneficiary" else self.invitee).client
        client.cookies.clear()
        response = login_with_privy_identity_token(client, token, expected_uid=uid, platform=APP_PLATFORM,
                                                   audit=self.credentials.audit)
        event = login_event(response, intent, uid)
        event["http"] = self.credentials.audit[-1]
        self.journal.save(action, event)
        session = self.credentials.connect(response["token"], uid)
        if role == "beneficiary":
            self.api = session
        else:
            self.invitee = session
        return event

    def bind_friend(self, baseline):
        name = "invite"
        accepted = self.journal.load(name)
        if accepted is not None:
            return accepted
        intent = self.journal.load(f"{name}.intent")
        recovering = intent is not None
        if not recovering:
            self.ready_to_write()
            code = self.api.get(f"{REFERRAL}/overview").get("invite_code")
            require(isinstance(code, str) and code, "邀请人未返回有效邀请码")
            ensure_window(self.case)
            intent = {"started_at": now().isoformat(), "idempotency_key": f"m06-{self.case['run_id']}"}
            self.journal.save(f"{name}.intent", intent)
            reply = self.invitee.call("POST", f"{REFERRAL}/inviter/confirm",
                                      json={"code": code, "idempotency_key": intent["idempotency_key"]})
            relation = reply.get("relation")
        else:
            # After an ambiguous POST, only read the invitee's durable relation.
            relation = self.invitee.get(f"{REFERRAL}/overview").get("inviter")
            require(relation, "邀请提交结果未确认，受邀方尚未查到绑定关系")
        require(isinstance(relation, dict) and number(relation["inviter_id"]) == self.case["uid"],
                "受邀方绑定结果未关联本轮邀请人")
        bound_at = utc(relation["bound_at"])
        earliest = max(utc(baseline["observed_at"]), utc(intent["started_at"])).replace(microsecond=0)
        require(earliest <= bound_at <= now(), "受邀方绑定时间不在本轮请求窗口")
        accepted = {"inviter_uid": self.case["uid"], "friend_uid": self.case["friend_uid"],
                    "bound_at": bound_at.isoformat(), "recovered_from_read": recovering}
        self.journal.save(name, accepted)
        return accepted

    def run(self):
        baseline = self.observe("baseline")
        scenario = self.case["scenario"]
        if scenario == "daily":
            day_one = date.fromisoformat(self.case["day_one"])
            first = self.app_login("day1_login", "beneficiary", self.case["uid"], day_one)
            progress = self.observe("progress", baseline, baseline, first)
            if now().astimezone(MANILA).date() == day_one:
                return "waiting_day_2"
            last = self.app_login("completion_login", "beneficiary", self.case["uid"], day_one + timedelta(days=1))
        elif scenario == "friends":
            binding = self.bind_friend(baseline)
            progress = self.observe("progress", baseline, baseline)
            require(utc(progress["friend"]["bound_at"]) == utc(binding["bound_at"]),
                    "受邀方绑定回执与邀请人好友进度不一致")
            last = self.app_login("completion_login", "friend", self.case["friend_uid"])
        else:
            progress = baseline
            last = self.app_login("completion_login", "beneficiary", self.case["uid"])
        self.observe("paid", baseline, progress, last)
        return "paid"


def flow_case(path):
    case = parse_case(path)
    config = read_json(path)
    for field in ("beneficiary_account", "friend_account"):
        if field in config:
            require(re.fullmatch(r"[A-Za-z0-9_-]+", config[field]), f"{field} 应为 ci.yaml 中的数据组")
            case[field] = config[field]
    if case["scenario"] == "daily":
        day_one = date.fromisoformat(config["day_one"])
        case["day_one"] = day_one.isoformat()
        require(manila_day(case["starts_at"]) <= case["day_one"] < manila_day(case["ends_at"]),
                "授权窗口须覆盖选定的 Manila Day 1 和 Day 2")
    if case.get("beneficiary_account") and case.get("friend_account"):
        require(case["beneficiary_account"] != case["friend_account"], "邀请双方应使用独立账号配置")
    return case


def run_flow(case, root, wait_seconds, replay_path=None):
    require(get_environment() == "preview", "M06 受控写入仅支持 Preview")
    journal = RunJournal(root, case)
    with ExitStack() as stack:
        uid, friend_uid = case["uid"], case.get("friend_uid")
        for account in sorted({uid} if friend_uid is None else {uid, friend_uid}):
            lock = stack.enter_context((root / f"uid-{account}.lock").open("a"))
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("同一专用账号已有 Rewards 流程在运行") from None
        frozen_case = journal.load("case")
        if frozen_case is None:
            journal.save("case", case)
        else:
            require(frozen_case == case, "run_id 已关联另一份运行配置")
        paid = journal.load("paid")
        if replay_path is not None:
            require(paid is not None, "完成发奖对账后才能核对原事件重投")
            replay = read_event(replay_path, case, "replay", paid)
            saved = journal.load("replay")
            require(saved is None or saved["event"] == replay, "现有重投证据关联另一份记录")
        elif paid is not None:
            return "paid"
        if paid is None and case["scenario"] == "daily" and journal.load("completion_login.intent") is None:
            today = now().astimezone(MANILA).date()
            day_one = date.fromisoformat(case["day_one"])
            require(day_one <= today <= day_one + timedelta(days=1), "当前日期不在本轮 Manila Day 1/Day 2")
            if journal.load("progress") is not None and today == day_one:
                return "waiting_day_2"
            require(today == day_one or journal.load("day1_login") is not None, "缺少本轮 Day 1 的真实登录记录")

        audit = []
        credentials = Credentials(case, audit, stack)
        attempt = {"started_at": now().isoformat(), "requests": audit}
        try:
            api = credentials.session("beneficiary", uid)
            invitee = credentials.session("friend", friend_uid) if friend_uid is not None else None
            flow = RewardsFlow(journal, api, invitee, credentials, wait_seconds)
            if replay_path is not None:
                flow.observe("replay", journal.load("baseline"), paid, replay)
                outcome = "replay_checked"
            else:
                outcome = flow.run()
            attempt["status"] = outcome
            return outcome
        except (ValueError, RuntimeError, KeyError, TypeError, pytest.fail.Exception) as error:
            attempt["status"] = "incomplete"
            attempt["error"] = str(error) if isinstance(error, (ValueError, RuntimeError)) else type(error).__name__
            raise
        finally:
            attempt["ended_at"] = now().isoformat()
            journal.save(f"attempt-{uuid4().hex}", attempt)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", type=Path, required=True)
    parser.add_argument("--journal", type=Path, default=Path(".local/rewards"))
    parser.add_argument("--wait-seconds", type=int, default=90)
    parser.add_argument("--replay-event", type=Path, help="Check ledger after a service-owned redelivery")
    args = parser.parse_args()
    try:
        require(get_environment() == "preview", "M06 受控写入仅支持 Preview")
        require(0 <= args.wait_seconds <= 600, "轮询预算须为 0 至 600 秒")
        case = flow_case(args.case)
        outcome = run_flow(case, args.journal, args.wait_seconds, args.replay_event)
    except (ValueError, RuntimeError, OSError, KeyError, TypeError, pytest.fail.Exception) as error:
        message = str(error) if isinstance(error, (ValueError, RuntimeError)) else type(error).__name__
        raise SystemExit(f"M06 未完成，{message}。详见运行目录。") from None
    print(f"M06 {outcome}，证据目录 {args.journal / case['run_id']}")


if __name__ == "__main__":
    main()
