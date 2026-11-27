"""Capture Preview M06 checkpoints around operator-run business events."""

import argparse
import json
import os
import re
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID
from zoneinfo import ZoneInfo

from function.api_client import create_client, request
from function.settings import get_environment


MANILA = ZoneInfo("Asia/Manila")
REWARDS = "/api/v1/rewards"
HISTORY = "/api/v1/profile/credit/history"
INBOX = "/api/v1/notifications/list"
PAGE_SIZE = 50
MAX_PAGES = 10
FIXTURE_MARKERS = ("preview display fixture only", "linked user api fixture; no spendable credit")
APP_LOGIN = "identity.app_login.v1"


class RewardsPending(ValueError):
    """The expected state has not reached the queried projection yet."""


def require(condition, message):
    if not condition:
        raise ValueError(message)


def number(value):
    if type(value) is int and value >= 0:
        return value
    require(isinstance(value, str) and re.fullmatch(r"[0-9]{1,20}", value), "期望非负整数")
    return int(value)


def utc(value):
    parsed = datetime.fromisoformat(value)
    require(parsed.utcoffset() == timedelta(0), "时间必须使用 UTC")
    return parsed


def now():
    return datetime.now(timezone.utc)


def one(items, message):
    matches = list(items)
    require(len(matches) == 1, message)
    return matches[0]


def one_published(items, message):
    matches = list(items)
    if not matches:
        raise RewardsPending(message)
    return one(matches, message)


def rows(body, key):
    items = body.get(key)
    require(isinstance(items, list) and all(isinstance(item, dict) for item in items), f"{key} 应为对象列表")
    return items


class RewardsAPI:
    def __init__(self, client, uid, audit):
        self.client = client
        self.audit = audit
        session = self.get("/api/v1/auth/session")
        require(number(session.get("auth_level")) in (1, 2) and number(session.get("user_id")) == uid,
                "会话不是本轮指定的 Preview UID")

    def get(self, path, **params):
        return self.call("GET", path, params=params)

    def call(self, method, path, **kwargs):
        self.client.cookies.clear()
        response = request(self.client, method, path, operation=path, **kwargs)
        self.audit.append({"method": method, "path": path, "http_status": response.status_code})
        require(response.status_code == 200, f"{path} 返回 HTTP {response.status_code}")
        body = response.json()
        require(isinstance(body, dict), f"{path} 应返回 JSON 对象")
        if "code" in body:
            require(type(body["code"]) is int and body["code"] == 0 and isinstance(body.get("data"), dict),
                    f"{path} 业务响应失败")
            return body["data"]
        return body

    def paged(self, path, key, param, next_field, cursor=None):
        items, seen = [], {cursor}
        for _ in range(MAX_PAGES):
            params = {} if cursor is None else {param: cursor}
            page = self.get(path, page_size=PAGE_SIZE, **params)
            items.extend(rows(page, key))
            has_more = page.get("has_more")
            require(type(has_more) is bool, f"{path} 缺少 has_more")
            if not has_more:
                return items
            next_cursor = page.get(next_field)
            if param == "after_revision":
                next_cursor = number(next_cursor)
                require(next_cursor > cursor, "回执 revision 未递增")
            else:
                require(isinstance(next_cursor, str) and next_cursor, f"{path} 缺少分页游标")
            require(next_cursor not in seen, f"{path} 分页游标重复")
            seen.add(next_cursor)
            cursor = next_cursor
        raise ValueError(f"{path} 超过取证分页上限")

    def notices(self):
        items = []
        for page in range(1, MAX_PAGES + 1):
            body = self.get(INBOX, type="credit_granted", page=page, page_size=PAGE_SIZE)
            total = number(body.get("total"))
            require(number(body.get("page")) == page and number(body.get("page_size")) == PAGE_SIZE,
                    "通知分页参数与请求不一致")
            page_rows = rows(body, "notifications")
            require(len(page_rows) == min(PAGE_SIZE, max(0, total - (page - 1) * PAGE_SIZE)),
                    "通知页条数变化，无法确认完整数量")
            items.extend(page_rows)
            if page * PAGE_SIZE >= total:
                return items
        raise ValueError("通知超过取证分页上限")


def check_task(detail, case):
    task = detail["task"]
    config = task["config"]
    require(task["id"] == case["task_id"] and number(task["version"]) == case["task_version"]
            and task.get("online") is True, "任务未上线或任务 ID / 版本漂移")
    require(detail.get("test_context") is None, "任务有活动的日期测试投影")
    rendered_detail = json.dumps(detail, ensure_ascii=False).lower()
    require(not any(marker in rendered_detail for marker in FIXTURE_MARKERS),
            "任务带展示 fixture 标记")
    require(config["type"] == case["scenario"] and config["pay_mode"] == "once"
            and number(config["reward_cents"]) == case["amount_cents"]
            and config.get("unlock", {}).get("kind") in (None, "", "none"), "任务类型、金额或解锁规则漂移")
    if case["scenario"] == "daily":
        challenge = config["challenge"]
        condition = challenge["condition"]
        require(number(challenge["need"]) == 2 and number(challenge["days"]) == 2
                and condition["action"] == "app_login" and number(condition["target"]) == 1,
                "只支持两个 Manila 自然日各登录一次的任务")
    elif case["scenario"] == "friends":
        condition = one(config["friend_requirements"], "本轮好友任务应只有一个条件")["condition"]
        require(condition["action"] == "app_login" and number(condition["target"]) == 1,
                "本轮好友条件应为一次 App 登录")
    else:
        condition = one(config["steps"], "代表性步骤任务应只有一个条件")["condition"]
        require(condition["action"] == "app_login" and number(condition["target"]) == 1,
                "代表性步骤任务应只要求一次 App 登录")


def daily_days(detail):
    challenge = detail.get("challenge") or {}
    days = [*challenge.get("days", []), challenge.get("today") or {}]
    return sorted({day["date"] for day in days
                   if day.get("state") == "qualified" or (day.get("counted") or {}).get("done") is True})


def payout(unit, receipts, batches, notices, case, after_revision, earliest, latest):
    require(len(receipts) <= 1, "本轮任务单元应有且仅有一张新回执")
    if unit is None or unit.get("state") != "paid":
        raise RewardsPending("任务单元尚未 paid")
    receipt = one_published(receipts, "本轮任务单元应有且仅有一张新回执")
    batch_id = number(receipt["batch_id"])
    amount = case["amount_cents"]
    paid_at = utc(receipt["paid_at"])
    require(batch_id > 0 and number(receipt["revision"]) > after_revision
            and number(receipt["amount_cents"]) == amount and earliest <= paid_at <= latest,
            "回执批次、金额、revision 或发放时间与本轮不一致")
    require(number(unit["batch_id"]) == batch_id and number(unit["amount_cents"]) == amount
            and utc(unit["paid_at"]) == paid_at, "任务 paid 单元与回执不一致")
    batch = one_published((row for row in batches if number(row["id"]) == batch_id),
                          "Credit 历史应只有一个关联批次")
    raw_amount = amount * 10_000
    require(number(batch["granted"]) == raw_amount and earliest <= utc(batch["granted_at"]) <= latest,
            "Credit 批次金额或发放时间与本轮不一致")
    notice = one_published((row for row in notices if row.get("related_type") == "credit_batch"
                            and number(row["related_id"]) == batch_id), "Inbox 应只有一条关联发奖通知")
    params = notice.get("message_params", {})
    require(notice.get("type") == "credit_granted" and notice.get("action_type") == "view_credit"
            and isinstance(params, dict) and params.get("task_id") == case["task_id"]
            and params.get("unit_id") == case["unit_id"]
            and number(params.get("batch_id")) == batch_id and number(params.get("amount")) == raw_amount,
            "通知批次、任务单元、到账金额或跳转不一致")
    return {"batch_id": batch_id, "amount_cents": amount, "receipt_revision": number(receipt["revision"]),
            "paid_at": receipt["paid_at"], "operation_id": receipt.get("operation_id"),
            "granted_raw": raw_amount, "granted_at": batch["granted_at"], "notice_id": notice.get("id")}


def friend_progress(api, invitee, case, phase):
    friends = api.paged(f"{REWARDS}/tasks/{case['task_id']}/friends", "friends", "page_token", "next_page_token")
    matches = [row for row in friends if number(row["user_id"]) == case["friend_uid"]]
    inviter = invitee.get("/api/v1/referral/v2/overview").get("inviter")
    if phase == "baseline":
        require(not matches and not inviter, "好友已绑定，无法记录本轮新的邀请关系")
        return {}
    friend = one_published(matches, "邀请人 Rewards 应只有一条本轮好友记录")
    if not inviter:
        raise RewardsPending("受邀方的邀请关系尚未可见")
    require(inviter and number(inviter["inviter_id"]) == case["uid"], "受邀方未确认同一邀请人")
    bound_at = utc(inviter["bound_at"])
    require(utc(friend["invited_at"]) == bound_at, "两侧邀请绑定时间不一致")
    step = one(rows(friend, "progress"), "好友进度应只有一个 App 登录条件")
    done = step.get("done") is True
    expects_done = phase in ("paid", "replay")
    if expects_done and not done:
        raise RewardsPending("好友 App 登录条件尚未达标")
    require(done == expects_done, "好友进度与本轮阶段不一致")
    return {"relation_id": friend["id"], "bound_at": bound_at.isoformat(), "done": done}


def checkpoint(api, invitee, case, phase, baseline, previous, event):
    task_id, unit_id = case["task_id"], case["unit_id"]
    overview = api.get(f"{REWARDS}/overview")
    require(overview.get("payouts_enabled") is True, "Preview Rewards 未开启真实发奖")
    revision = number(overview.get("receipt_revision"))
    detail = api.get(f"{REWARDS}/tasks/{task_id}")
    check_task(detail, case)

    units = [row for row in rows(detail, "rewards") if row.get("id") == unit_id]
    require(len(units) <= 1, "同一任务奖励单元返回多次")
    unit = units[0] if units else None
    after = baseline.get("receipt_revision", revision)
    receipts = api.paged(f"{REWARDS}/receipts", "receipts", "after_revision", "next_revision", after)
    matching = [row for row in receipts if (row.get("task_id"), row.get("unit_id")) == (task_id, unit_id)]
    batches = api.paged(HISTORY, "batches", "cursor", "next_cursor")
    batch_ids = sorted(number(row["id"]) for row in batches)
    require(len(set(batch_ids)) == len(batch_ids), "Credit 历史出现重复批次")

    result = {"receipt_revision": revision, "batch_ids": batch_ids}
    if case["scenario"] == "daily":
        result["qualified_days"] = daily_days(detail)
        result["round_id"] = (detail.get("challenge") or {}).get("round_id")
        if phase in ("paid", "replay") and previous.get("round_id"):
            require(result["round_id"] == previous["round_id"], "任务轮次在取证期间发生变化")
        if phase != "replay":
            dates = [] if phase == "baseline" else [manila_day(event["occurred_at"])]
            if phase == "paid":
                first = manila_day(previous["event"]["occurred_at"])
                require(datetime.fromisoformat(dates[0]).date() - datetime.fromisoformat(first).date() == timedelta(days=1),
                        "两次登录没有发生在连续的 Manila 自然日")
                dates.insert(0, first)
        else:
            dates = previous["qualified_days"]
        require(set(result["qualified_days"]).issubset(dates), "服务端达标自然日与本轮 App 事件不一致")
        if result["qualified_days"] != dates:
            raise RewardsPending("服务端尚未记录本轮 App 登录的达标自然日")
    elif case["scenario"] == "friends":
        result["friend"] = friend_progress(api, invitee, case, phase)
        if phase != "baseline":
            bound_at = utc(result["friend"]["bound_at"])
            require(utc(baseline["observed_at"]).replace(microsecond=0) <= bound_at, "邀请早于本轮基线")
            if phase in ("paid", "replay"):
                require(all(result["friend"][key] == previous["friend"][key] for key in ("relation_id", "bound_at"))
                        and bound_at.replace(microsecond=0) <= utc(event["occurred_at"]),
                        "好友归属变化或 App 登录早于绑定")
    else:
        step = one(rows(detail, "progress"), "代表性步骤任务应只有一项进度")
        result["step_done"] = step.get("done") is True
        if phase in ("paid", "replay") and not result["step_done"]:
            raise RewardsPending("代表性步骤任务尚未达标")
        if phase in ("baseline", "progress"):
            require(not result["step_done"], "代表性步骤任务已有 App 登录进度")

    if phase in ("baseline", "progress"):
        require(not matching and (unit is None or unit.get("state") != "paid"), "付奖早于本轮达标")
        return result

    result["payout"] = payout(unit, matching, batches, api.notices(), case, after,
                              utc(event["occurred_at"]), now())
    require(result["payout"]["batch_id"] not in baseline["batch_ids"], "本轮使用了基线已有 Credit 批次")
    if phase == "replay":
        require(result["payout"] == previous["payout"] and batch_ids == previous["batch_ids"],
                "同一事件重放后回执、批次或通知发生变化")
    return result


def manila_day(value):
    return utc(value).astimezone(MANILA).date().isoformat()


def read_json(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    require(isinstance(data, dict), "输入必须为 JSON 对象")
    return data


def parse_case(path):
    raw = read_json(path)
    case = {key: raw[key] for key in ("run_id", "scenario", "task_id", "service_revision",
                                     "starts_at", "ends_at", "uid", "task_version", "amount_cents")}
    case["run_id"] = str(UUID(case["run_id"]))
    require(case["scenario"] in ("daily", "friends", "steps"), "场景应为 daily、friends 或 steps")
    require(re.fullmatch(r"[A-Za-z0-9_-]{1,80}", case["task_id"]), "task_id 无效")
    require(re.fullmatch(r"[A-Za-z0-9._-]{1,128}", case["service_revision"]), "缺少 Preview 服务版本")
    require(utc(case["starts_at"]) <= now() < utc(case["ends_at"]), "当前时间不在本轮 Preview 窗口")
    for key in ("uid", "amount_cents", "task_version"):
        case[key] = number(case[key])
        require(case[key] > 0, f"{key} 必须为正整数")
    case["unit_id"] = "once"
    if case["scenario"] == "friends":
        case["friend_uid"] = number(raw["friend_uid"])
        require(case["friend_uid"] > 0 and case["uid"] != case["friend_uid"], "好友必须是独立的专用 UID")
        case["unit_id"] = f"friend:{case['friend_uid']}"
    return case


def prior(path, case, phase):
    require(path is not None, f"缺少 {phase} 证据")
    evidence = read_json(path)
    require(evidence.get("case") == case and evidence.get("phase") == phase and evidence.get("status") == "observed",
            f"{phase} 证据与本轮不匹配")
    return evidence


def read_event(path, case, phase, previous):
    required = phase in ("paid", "replay") or (phase == "progress" and case["scenario"] == "daily")
    if not required:
        require(path is None, "此阶段无需 App 登录事件")
        return None
    require(path is not None, "缺少服务端 App 登录事件取证")
    raw = read_json(path)
    event = {key: raw[key] for key in ("event_id", "type", "uid", "occurred_at", "source_ref")}
    event["uid"] = number(event["uid"])
    require(event["type"] == APP_LOGIN and event["uid"] == case.get("friend_uid", case["uid"]),
            "登录事件类型或触发 UID 不匹配")
    for key in ("event_id", "source_ref"):
        require(re.fullmatch(r"[A-Za-z0-9._:-]{1,160}", event[key]), f"{key} 应为脱敏事件索引")
    if phase == "replay":
        original = previous["event"]
        if "event_id" in original:
            require(event == original, "重放必须使用原发奖事件")
        else:
            require(event["type"] == original["type"] and event["uid"] == original["uid"]
                    and utc(event["occurred_at"]).replace(microsecond=0) == utc(original["occurred_at"]),
                    "重放事件与本轮原登录 UID 或服务端时间不一致")
        delivery_id, delivered_at = raw["delivery_id"], utc(raw["delivered_at"])
        processed_at = utc(raw["processed_at"])
        require(re.fullmatch(r"[A-Za-z0-9._:-]{1,160}", delivery_id) and delivery_id != event["event_id"]
                and utc(previous["observed_at"]) <= delivered_at <= processed_at <= now(),
                "缺少本轮重放投递和处理凭据")
        return {**event, "delivery_id": delivery_id, "delivered_at": raw["delivered_at"],
                "processed_at": raw["processed_at"]}
    require(utc(previous["observed_at"]) <= utc(event["occurred_at"]) <= now(),
            "事件时间应在前一阶段取证之后")
    if previous.get("event"):
        require(event["event_id"] != previous["event"]["event_id"], "连续两天应各有独立的真实 App 登录事件")
    return event


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("baseline", "progress", "paid", "replay"))
    for name in ("case", "out", "baseline", "previous", "event"):
        parser.add_argument(f"--{name}", type=Path, required=name in ("case", "out"))
    args = parser.parse_args()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("x", encoding="utf-8") as stream:
        result = {"phase": args.phase, "started_at": now().isoformat(), "requests": []}
        try:
            require(get_environment() == "preview", "M06 取证仅支持 Preview")
            case = parse_case(args.case)
            result["case"] = case
            baseline = {} if args.phase == "baseline" else prior(args.baseline, case, "baseline")
            previous_phase = {"paid": "progress", "replay": "paid"}.get(args.phase)
            previous = prior(args.previous, case, previous_phase) if previous_phase else baseline
            require(not previous_phase or previous.get("baseline_at") == baseline["observed_at"],
                    "前置证据使用了不同的基线")
            event = read_event(args.event, case, args.phase, previous)
            result.update(event=event, baseline_at=baseline.get("observed_at"))
            with ExitStack() as stack:
                def connect(key, uid):
                    token = os.environ.get(key, "")
                    require(re.fullmatch(r"[A-Za-z0-9_.~+/=-]+", token), f"缺少有效的 {key}")
                    client = stack.enter_context(create_client(bearer_token=token))
                    return RewardsAPI(client, uid, result["requests"])

                api = connect("BUZZING_REWARDS_TOKEN", case["uid"])
                invitee = connect("BUZZING_REWARDS_FRIEND_TOKEN", case["friend_uid"]) if "friend_uid" in case else None
                result.update(checkpoint(api, invitee, case, args.phase, baseline, previous, event))
            result["status"] = "observed"
        except (ValueError, KeyError, TypeError, RuntimeError) as error:
            result["status"] = "incomplete"
            result["error"] = str(error) if isinstance(error, (ValueError, RuntimeError)) else type(error).__name__
        finally:
            result["observed_at"] = now().isoformat()
            json.dump(result, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    if result["status"] != "observed":
        raise SystemExit(f"取证未完成，见 {args.out}")
    print(f"已记录 {args.phase}，见 {args.out}")


if __name__ == "__main__":
    main()
