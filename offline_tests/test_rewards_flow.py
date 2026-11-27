import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from function import rewards_flow, rewards_run


class PreviewRewards:
    def __init__(self, monkeypatch, scenario):
        self.clock = datetime(2026, 11, 27, 2, tzinfo=timezone.utc)
        self.scenario = scenario
        self.writes = []
        self.events = []
        self.bound_at = None
        self.hide_notice = False
        self.lose_login_reply = False
        self.lose_binding_reply = False
        self.case = {
            "run_id": "8307eff7-6396-42b9-bbd6-b12b8c82d49b", "scenario": scenario, "uid": 123,
            "task_id": "preview-rewards", "task_version": 3, "amount_cents": 2,
            "service_revision": "preview-contract", "unit_id": "once",
            "starts_at": "2026-11-27T00:00:00Z", "ends_at": "2026-11-30T00:00:00Z",
        }
        if scenario == "friends":
            self.case.update(friend_uid=456, unit_id="friend:456")
        if scenario == "daily":
            self.case["day_one"] = "2026-11-27"
        monkeypatch.setenv("BUZZING_ENV", "preview")
        monkeypatch.setenv("BUZZING_REWARDS_TOKEN", "beneficiary")
        monkeypatch.setenv("BUZZING_REWARDS_FRIEND_TOKEN", "friend")
        monkeypatch.setenv("BUZZING_REWARDS_PRIVY_IDENTITY_TOKEN", "proof-beneficiary")
        monkeypatch.setenv("BUZZING_REWARDS_FRIEND_PRIVY_IDENTITY_TOKEN", "proof-friend")
        monkeypatch.setattr(rewards_flow, "now", lambda: self.clock)
        monkeypatch.setattr(rewards_run, "now", lambda: self.clock)
        monkeypatch.setattr(rewards_flow, "create_client", self.client)

    def client(self, bearer_token=None):
        headers = {"Authorization": f"Bearer {bearer_token}"} if bearer_token else {}
        return httpx.Client(base_url="https://preview-api.buzzing.app", headers=headers,
                            transport=httpx.MockTransport(self.endpoint))

    @property
    def paid(self):
        return len(self.events) >= (2 if self.scenario == "daily" else 1)

    def receipt(self):
        return {"task_id": self.case["task_id"], "unit_id": self.case["unit_id"], "amount_cents": "2",
                "paid_at": self.events[-1], "revision": "18", "batch_id": "81", "operation_id": "payment-1"}

    def task(self):
        condition = {"action": "app_login", "target": "1"}
        config = {"type": self.scenario, "pay_mode": "once", "reward_cents": "2",
                  "friend_requirements": [{"condition": condition}], "steps": [{"condition": condition}],
                  "challenge": {"days": 2, "need": 2, "condition": condition}}
        units = [{"id": self.case["unit_id"], "state": "paid", **self.receipt()}] if self.paid else []
        return {"task": {"id": self.case["task_id"], "version": "3", "online": True, "config": config},
                "rewards": units, "progress": [{"done": self.paid}],
                "challenge": {"round_id": "challenge-1", "days": [
                    {"date": rewards_run.manila_day(timestamp), "state": "qualified"} for timestamp in self.events
                ]}}

    def post(self, req, uid):
        path = req.url.path
        payload = json.loads(req.content)
        self.writes.append((path, payload))
        if path.endswith("/inviter/confirm"):
            assert uid == 456
            assert payload == {"code": "USER-INVITE", "idempotency_key": f"m06-{self.case['run_id']}"}
            self.bound_at = self.clock.isoformat()
            if self.lose_binding_reply:
                raise httpx.ReadTimeout("response lost")
            return {"relation": {"inviter_id": "123", "bound_at": self.bound_at}}
        assert path == "/api/v1/login"
        if "platform" not in payload:
            assert uid is None
            expected_uid = int(payload["expect_user_id"])
            tag = "beneficiary" if expected_uid == 123 else "friend"
            assert payload == {"expect_user_id": str(expected_uid), "privy_identity_token": f"proof-{tag}"}
            return {"user_id": expected_uid, "token": tag, "expire": "2026-12-01T00:00:00Z"}
        friend = self.scenario == "friends"
        expected_uid, proof = (456, "proof-friend") if friend else (123, "proof-beneficiary")
        assert uid == expected_uid
        assert payload == {"platform": "ios", "expect_user_id": str(expected_uid), "privy_identity_token": proof}
        assert not friend or self.bound_at is not None
        self.events.append(self.clock.isoformat())
        if self.lose_login_reply:
            raise httpx.ReadTimeout("response lost")
        return {"user_id": expected_uid, "auth_at": int(self.clock.timestamp()),
                "token": "friend" if friend else "beneficiary", "expire": "2026-12-01T00:00:00Z"}

    def endpoint(self, req):
        path = req.url.path
        uid = {"Bearer beneficiary": 123, "Bearer friend": 456}.get(req.headers.get("Authorization"))
        if req.method == "POST":
            return httpx.Response(200, json=self.post(req, uid))
        assert uid is not None
        if path == "/api/v1/auth/session":
            reply = {"auth_level": 2, "user_id": str(uid)}
        elif path == "/api/v1/referral/v2/overview":
            reply = {"invite_code": "USER-INVITE"} if uid == 123 else {
                "inviter": {"inviter_id": "123", "bound_at": self.bound_at} if self.bound_at else None
            }
        else:
            assert uid == 123
            reply = self.rewards_reply(path, req.url.params)
        return httpx.Response(200, json=reply)

    def rewards_reply(self, path, params):
        if path == f"{rewards_run.REWARDS}/overview":
            return {"payouts_enabled": True, "receipt_revision": "18" if self.paid else "17"}
        if path.endswith("/friends"):
            friends = [{"id": "binding-1", "user_id": "456", "invited_at": self.bound_at,
                        "progress": [{"done": bool(self.events)}]}] if self.bound_at else []
            return {"friends": friends, "has_more": False}
        if path.endswith("/receipts"):
            receipts = [self.receipt()] if self.paid and params["after_revision"] == "17" else []
            return {"receipts": receipts, "has_more": False}
        if path == rewards_run.HISTORY:
            batches = [{"id": "81", "granted": "20000", "granted_at": self.events[-1]}] if self.paid else []
            return {"batches": batches, "has_more": False}
        if path == rewards_run.INBOX:
            assert params["type"] == "credit_granted"
            notices = [{"id": "82", "type": "credit_granted", "related_type": "credit_batch", "related_id": "81",
                        "action_type": "view_credit", "message_params": {"batch_id": "81", "amount": "20000",
                        "task_id": self.case["task_id"], "unit_id": self.case["unit_id"]}}] if not self.hide_notice else []
            return {"code": 0, "data": {"notifications": notices, "total": len(notices), "page": 1, "page_size": 50}}
        assert path == f"{rewards_run.REWARDS}/tasks/{self.case['task_id']}"
        return self.task()


def test_daily_flow_waits_for_the_next_manila_date(monkeypatch, tmp_path):
    preview = PreviewRewards(monkeypatch, "daily")
    assert rewards_flow.run_flow(preview.case, tmp_path, 0) == "waiting_day_2"
    assert rewards_flow.run_flow(preview.case, tmp_path, 0) == "waiting_day_2"
    assert len(preview.writes) == 1

    preview.clock += timedelta(days=1)
    assert rewards_flow.run_flow(preview.case, tmp_path, 0) == "paid"
    assert rewards_flow.run_flow(preview.case, tmp_path, 0) == "paid"
    assert len(preview.writes) == 2
    paid = rewards_flow.RunJournal(tmp_path, preview.case).load("paid")
    assert paid["payout"]["batch_id"] == 81
    assert paid["qualified_days"] == ["2026-11-27", "2026-11-28"]


def test_friends_writes_binding_and_login_with_both_identities(monkeypatch, tmp_path):
    preview = PreviewRewards(monkeypatch, "friends")
    assert rewards_flow.run_flow(preview.case, tmp_path, 0) == "paid"
    assert [path for path, _ in preview.writes] == ["/api/v1/referral/v2/inviter/confirm", "/api/v1/login"]
    journal = rewards_flow.RunJournal(tmp_path, preview.case)
    assert (journal.load("invite")["inviter_uid"], journal.load("invite")["friend_uid"]) == (123, 456)
    assert journal.load("progress")["friend"]["done"] is False
    assert journal.load("paid")["friend"]["done"] is True
    evidence = "\n".join(path.read_text() for path in journal.directory.glob("*.json"))
    assert "USER-INVITE" not in evidence and "proof-friend" not in evidence


def test_representative_task_resumes_queries_if_inbox_is_delayed(monkeypatch, tmp_path):
    preview = PreviewRewards(monkeypatch, "steps")
    preview.hide_notice = True
    with pytest.raises(rewards_run.RewardsPending, match="Inbox"):
        rewards_flow.run_flow(preview.case, tmp_path, 0)
    preview.hide_notice = False
    assert rewards_flow.run_flow(preview.case, tmp_path, 0) == "paid"
    assert len(preview.writes) == 1


def test_unknown_login_is_never_resubmitted(monkeypatch, tmp_path):
    preview = PreviewRewards(monkeypatch, "steps")
    preview.lose_login_reply = True
    with pytest.raises(RuntimeError, match="ReadTimeout"):
        rewards_flow.run_flow(preview.case, tmp_path, 0)
    with pytest.raises(ValueError, match="请求结果未确认"):
        rewards_flow.run_flow(preview.case, tmp_path, 0)
    assert len(preview.writes) == 1


def test_lost_binding_response_recovers_from_invitee_relation(monkeypatch, tmp_path):
    preview = PreviewRewards(monkeypatch, "friends")
    preview.lose_binding_reply = True
    with pytest.raises(RuntimeError, match="ReadTimeout"):
        rewards_flow.run_flow(preview.case, tmp_path, 0)
    assert rewards_flow.run_flow(preview.case, tmp_path, 0) == "paid"
    journal = rewards_flow.RunJournal(tmp_path, preview.case)
    assert journal.load("invite")["recovered_from_read"]
    assert [path for path, _ in preview.writes] == ["/api/v1/referral/v2/inviter/confirm", "/api/v1/login"]


def test_today_cannot_replace_a_missed_day_one(monkeypatch, tmp_path):
    preview = PreviewRewards(monkeypatch, "daily")
    preview.clock += timedelta(days=1)
    with pytest.raises(ValueError, match="缺少本轮 Day 1"):
        rewards_flow.run_flow(preview.case, tmp_path, 0)
    assert not preview.writes


def test_session_bootstrap_and_business_login_use_distinct_requests(monkeypatch, tmp_path):
    preview = PreviewRewards(monkeypatch, "steps")
    monkeypatch.delenv("BUZZING_REWARDS_TOKEN")
    assert rewards_flow.run_flow(preview.case, tmp_path, 0) == "paid"
    assert [payload.get("platform") for _, payload in preview.writes] == [None, "ios"]
    assert len(preview.events) == 1


def test_replay_evidence_matches_original_login_and_preserves_payout(monkeypatch, tmp_path):
    preview = PreviewRewards(monkeypatch, "steps")
    assert rewards_flow.run_flow(preview.case, tmp_path, 0) == "paid"
    preview.clock += timedelta(minutes=1)
    replay = tmp_path / "outbox.json"
    evidence = {"event_id": "identity-event", "source_ref": "outbox-row", "type": rewards_run.APP_LOGIN,
                "uid": 123, "occurred_at": preview.events[0], "delivery_id": "delivery-2",
                "delivered_at": preview.clock.isoformat(), "processed_at": preview.clock.isoformat()}
    replay.write_text(json.dumps(evidence), encoding="utf-8")
    assert rewards_flow.run_flow(preview.case, tmp_path, 0, replay) == "replay_checked"
    assert len(preview.writes) == 1
    evidence["occurred_at"] = preview.clock.isoformat()
    replay.write_text(json.dumps(evidence), encoding="utf-8")
    with pytest.raises(ValueError, match="原登录 UID 或服务端时间不一致"):
        rewards_flow.run_flow(preview.case, tmp_path, 0, replay)
