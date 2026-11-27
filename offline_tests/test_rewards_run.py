from copy import deepcopy
from datetime import datetime, timezone

import pytest

from function.rewards_run import check_task, checkpoint, friend_progress, manila_day, payout


DAY_ONE = "2026-11-26T15:59:00Z"
DAY_TWO = "2026-11-26T16:01:00Z"
PAID = "2026-11-26T16:02:00Z"
CASE = {"uid": 123, "scenario": "daily", "task_id": "qa-daily", "task_version": 3,
        "unit_id": "once", "amount_cents": 2}


def test_manila_boundary_is_at_16_utc():
    assert manila_day(DAY_ONE) == "2026-11-26"
    assert manila_day(DAY_TWO) == "2026-11-27"


class DailyAPI:
    def __init__(self, count):
        self.count = count
        self.days = ["2026-11-26", "2026-11-27"][:count]
        self.receipt = {"task_id": "qa-daily", "unit_id": "once", "batch_id": "81",
                        "revision": "18", "amount_cents": "2", "paid_at": PAID}
        self.batch = {"id": "81", "granted": "20000", "granted_at": PAID}
        self.notice = {"id": "82", "type": "credit_granted", "action_type": "view_credit",
                       "related_type": "credit_batch", "related_id": "81",
                       "message_params": {"task_id": "qa-daily", "unit_id": "once",
                                          "batch_id": "81", "amount": "20000"}}
        units = [{"id": "once", "state": "paid", **self.receipt}] if count == 2 else []
        self.detail = {
            "task": {"id": "qa-daily", "version": "3", "online": True, "config": {
                "type": "daily", "pay_mode": "once", "reward_cents": "2",
                "challenge": {"need": 2, "days": 2, "condition": {"action": "app_login", "target": "1"}},
            }},
            "rewards": units,
            "challenge": {"days": [{"date": date, "state": "qualified"} for date in self.days]},
        }

    def get(self, path):
        if path.endswith("/overview"):
            return {"payouts_enabled": True, "receipt_revision": "18" if self.count == 2 else "17"}
        return self.detail

    def paged(self, path, key, *_):
        if self.count != 2:
            return []
        return [self.receipt] if key == "receipts" else [self.batch]

    def notices(self):
        return [self.notice]


def test_daily_checkpoints_and_replay_keep_one_batch():
    baseline = checkpoint(DailyAPI(0), None, CASE, "baseline", {}, {}, None)
    first_event = {"occurred_at": DAY_ONE}
    progress = checkpoint(DailyAPI(1), None, CASE, "progress", baseline, baseline, first_event)
    progress["event"] = first_event
    second_event = {"occurred_at": DAY_TWO}
    settled = DailyAPI(2)
    paid = checkpoint(settled, None, CASE, "paid", baseline, progress, second_event)
    assert paid["payout"]["batch_id"] == 81
    assert checkpoint(settled, None, CASE, "replay", baseline, paid, second_event) == paid

    with pytest.raises(ValueError, match="连续的 Manila 自然日"):
        checkpoint(settled, None, CASE, "paid", baseline, progress, first_event)


def test_friend_evidence_must_agree_on_inviter_and_binding_time():
    case = {**CASE, "friend_uid": 456}
    friend = {"id": "relation-789", "user_id": "456", "invited_at": DAY_ONE, "progress": [{"done": True}]}

    class InviterAPI:
        def paged(self, *_):
            return [friend]

    class InviteeAPI:
        def __init__(self, inviter_uid):
            self.inviter_uid = inviter_uid

        def get(self, _):
            return {"inviter": {"inviter_id": self.inviter_uid, "bound_at": DAY_ONE}}

    assert friend_progress(InviterAPI(), InviteeAPI("123"), case, "paid")["relation_id"] == "relation-789"
    with pytest.raises(ValueError, match="受邀方未确认同一邀请人"):
        friend_progress(InviterAPI(), InviteeAPI("999"), case, "paid")


@pytest.mark.parametrize("marker", ["Preview display fixture only", "Linked user API fixture; no spendable Credit"])
def test_fixture_cannot_supply_rewards_evidence(marker):
    detail = DailyAPI(0).detail
    detail["task"]["config"]["summary"] = marker
    with pytest.raises(ValueError, match="展示 fixture"):
        check_task(detail, CASE)


@pytest.mark.parametrize("duplicate", ["receipt", "batch", "notice"])
def test_duplicate_payout_evidence_fails(duplicate):
    api = DailyAPI(2)
    objects = {"receipt": [api.receipt], "batch": [api.batch], "notice": [api.notice]}
    objects[duplicate].append(deepcopy(objects[duplicate][0]))
    with pytest.raises(ValueError, match="一"):
        payout(api.detail["rewards"][0], objects["receipt"], objects["batch"], objects["notice"], CASE, 17,
               datetime.fromisoformat(DAY_TWO), datetime(2026, 11, 27, tzinfo=timezone.utc))
