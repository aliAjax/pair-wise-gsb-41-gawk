import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import CatastropheClaimService, DomainError  # noqa: E402
from suspension import DEADLINE_DAYS, deadline_snapshot, parse_ts  # noqa: E402

T0 = "2026-09-01T00:00:00+00:00"


def iso(dt):
    return dt.isoformat(timespec="seconds")


class DeadlinePureLogicTest(unittest.TestCase):
    def event(self, start, end=None, no="EV-1", name="台风海燕", regions=("A区",)):
        return {
            "event_no": no, "name": name, "kind": "typhoon",
            "regions": list(regions), "started_at": start,
            "ended_at": end,
        }

    def test_original_deadline_is_15_days_after_acceptance(self):
        snap = deadline_snapshot(T0, [], now="2026-09-02T00:00:00+00:00")
        self.assertEqual(snap["original_deadline"], "2026-09-16T00:00:00+00:00")
        self.assertEqual(snap["current_deadline"], "2026-09-16T00:00:00+00:00")
        self.assertFalse(snap["overdue"])
        self.assertEqual(snap["windows"], [])

    def test_active_event_freezes_clock_and_prevents_overdue(self):
        # 原期限第 10 天台风开始且未解除；第 20 天查看，不应判逾期
        start = "2026-09-11T00:00:00+00:00"
        now = "2026-09-21T00:00:00+00:00"
        snap = deadline_snapshot(T0, [self.event(start)], now=now)
        self.assertTrue(snap["suspended"])
        self.assertFalse(snap["overdue"])
        self.assertEqual(snap["paused_seconds"], 10 * 86400)
        self.assertEqual(snap["current_deadline"], "2026-09-26T00:00:00+00:00")

    def test_lifted_event_extends_by_actual_pause(self):
        start = "2026-09-11T00:00:00+00:00"
        end = "2026-09-14T00:00:00+00:00"  # 暂停 3 天
        snap = deadline_snapshot(T0, [self.event(start, end)], now="2026-09-21T00:00:00+00:00")
        self.assertFalse(snap["suspended"])
        self.assertEqual(snap["paused_seconds"], 3 * 86400)
        self.assertEqual(snap["current_deadline"], "2026-09-19T00:00:00+00:00")
        # 解除后恢复走表，第 20 天已逾期
        self.assertTrue(snap["overdue"])

    def test_overlapping_events_never_double_count(self):
        events = [
            self.event("2026-09-11T00:00:00+00:00", "2026-09-16T00:00:00+00:00", no="EV-1"),
            self.event("2026-09-14T00:00:00+00:00", "2026-09-20T00:00:00+00:00", no="EV-2"),
        ]
        snap = deadline_snapshot(T0, events, now="2026-09-25T00:00:00+00:00")
        # 并集 9/11–9/20 共 9 天，不是 5+6=11 天
        self.assertEqual(len(snap["windows"]), 1)
        self.assertEqual(snap["paused_seconds"], 9 * 86400)
        self.assertEqual(snap["current_deadline"], "2026-09-25T00:00:00+00:00")
        labels = [e["event_no"] for e in snap["windows"][0]["events"]]
        self.assertEqual(labels, ["EV-1", "EV-2"])

    def test_adjacent_events_merge_as_single_interval(self):
        events = [
            self.event("2026-09-11T00:00:00+00:00", "2026-09-14T00:00:00+00:00", no="EV-1"),
            self.event("2026-09-14T00:00:00+00:00", "2026-09-17T00:00:00+00:00", no="EV-2"),
        ]
        snap = deadline_snapshot(T0, events, now="2026-09-20T00:00:00+00:00")
        self.assertEqual(len(snap["windows"]), 1)
        self.assertEqual(snap["paused_seconds"], 6 * 86400)

    def test_separate_events_each_extend(self):
        events = [
            self.event("2026-09-03T00:00:00+00:00", "2026-09-05T00:00:00+00:00", no="EV-1"),
            self.event("2026-09-08T00:00:00+00:00", "2026-09-10T00:00:00+00:00", no="EV-2"),
        ]
        snap = deadline_snapshot(T0, events, now="2026-09-20T00:00:00+00:00")
        self.assertEqual(len(snap["windows"]), 2)
        self.assertEqual(snap["paused_seconds"], 4 * 86400)
        self.assertEqual(snap["current_deadline"], "2026-09-20T00:00:00+00:00")

    def test_event_after_paused_expiry_but_lifted_earlier_cascades(self):
        # 第一个事件把期限顶到 9/21；9/18-9/20 的第二个事件全程在新期限之内，
        # 继续顺延 2 天，期限级联到 9/23（共暂停 5+2=7 天）
        events = [
            self.event("2026-09-11T00:00:00+00:00", "2026-09-16T00:00:00+00:00", no="EV-1"),
            self.event("2026-09-18T00:00:00+00:00", "2026-09-20T00:00:00+00:00", no="EV-2"),
        ]
        snap = deadline_snapshot(T0, events, now="2026-09-25T00:00:00+00:00")
        self.assertEqual(snap["paused_seconds"], 7 * 86400)
        self.assertEqual(snap["current_deadline"], "2026-09-23T00:00:00+00:00")
    def test_event_before_acceptance_is_clipped(self):
        events = [
            self.event("2026-08-30T00:00:00+00:00", "2026-09-02T12:00:00+00:00"),
        ]
        snap = deadline_snapshot(T0, events, now="2026-09-03T00:00:00+00:00")
        # 只统计受理后 1.5 天
        self.assertEqual(snap["paused_seconds"], int(1.5 * 86400))
        self.assertEqual(snap["windows"][0]["start"], "2026-09-01T00:00:00+00:00")

    def test_event_starting_after_deadline_expiry_no_relief(self):
        start = "2026-09-20T00:00:00+00:00"
        snap = deadline_snapshot(T0, [self.event(start)], now="2026-09-22T00:00:00+00:00")
        self.assertEqual(snap["paused_seconds"], 0)
        self.assertEqual(snap["windows"], [])
        self.assertTrue(snap["overdue"])

    def test_future_dated_event_is_not_yet_active(self):
        start = "2026-09-10T00:00:00+00:00"
        end = "2026-09-12T00:00:00+00:00"
        snap = deadline_snapshot(T0, [self.event(start, end)], now="2026-09-05T00:00:00+00:00")
        self.assertEqual(snap["paused_seconds"], 0)
        self.assertEqual(snap["current_deadline"], "2026-09-16T00:00:00+00:00")
        self.assertFalse(snap["suspended"])
        self.assertFalse(snap["overdue"])

    def test_deadline_constant_is_15_days(self):
        self.assertEqual(DEADLINE_DAYS, 15)


class SuspensionServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = CatastropheClaimService(Path(self.tmp.name) / "test.db")
        self.accepted = datetime.now(timezone.utc) - timedelta(days=20)
        self.claim = self._create_claim("CLM-S-001", "A区")

    def tearDown(self):
        self.tmp.cleanup()

    def _create_claim(self, number, region, lat=30.1):
        return self.service.create_claim(
            "intake1", "intake", number, "TY2026", region, "typhoon",
            "P-" + number, "R-" + number, lat, 121.1, 300000, True, True,
        )

    def _set_created_at(self, claim_id, dt):
        with self.service.connect() as conn:
            conn.execute("UPDATE claims SET created_at=? WHERE id=?", (iso(dt), claim_id))

    def _detail_deadline(self, claim_id):
        return self.service.claim_detail("sup1", "supervisor", claim_id)["deadline"]

    def test_register_requires_supervisor(self):
        with self.assertRaises(DomainError) as ctx:
            self.service.register_suspension_event(
                "adj1", "adjuster", "EV-X", "台风", "typhoon", ["A区"], iso(self.accepted),
            )
        self.assertEqual(403, ctx.exception.status)

    def test_register_validates_regions_and_times(self):
        with self.assertRaises(DomainError):
            self.service.register_suspension_event(
                "sup1", "supervisor", "EV-X", "台风", "typhoon", [], iso(self.accepted),
            )
        future = datetime.now(timezone.utc) + timedelta(days=1)
        with self.assertRaises(DomainError):
            self.service.register_suspension_event(
                "sup1", "supervisor", "EV-X", "台风", "typhoon", ["A区"], iso(future),
            )
        with self.assertRaises(DomainError):
            self.service.register_suspension_event(
                "sup1", "supervisor", "EV-X", "台风", "typhoon", ["A区"],
                iso(self.accepted), iso(self.accepted - timedelta(hours=1)),
            )

    def test_active_registered_event_freezes_region_claims(self):
        start = self.accepted + timedelta(days=10)
        self._set_created_at(self.claim["id"], self.accepted)
        self.service.register_suspension_event(
            "sup1", "supervisor", "EV-001", "台风海燕", "typhoon", ["A区"], iso(start),
        )
        snap = self._detail_deadline(self.claim["id"])
        self.assertTrue(snap["suspended"])
        self.assertFalse(snap["overdue"])
        self.assertEqual(len(snap["windows"]), 1)
        self.assertEqual(snap["original_deadline"], iso(self.accepted + timedelta(days=15)))
        # 当前期限 = 原期限 + 事件开始至今的真实时长
        expected_pause = (datetime.now(timezone.utc) - start).total_seconds()
        self.assertAlmostEqual(snap["paused_seconds"], expected_pause, delta=300)

    def test_lift_extends_deadline_by_real_pause(self):
        self._set_created_at(self.claim["id"], self.accepted)
        start = self.accepted + timedelta(days=10)
        end = start + timedelta(days=3)
        self.service.register_suspension_event(
            "sup1", "supervisor", "EV-002", "地震", "earthquake", ["A区"], iso(start), iso(end),
        )
        snap = self._detail_deadline(self.claim["id"])
        self.assertFalse(snap["suspended"])
        self.assertEqual(snap["paused_seconds"], 3 * 86400)
        self.assertEqual(snap["current_deadline"], iso(self.accepted + timedelta(days=18)))

    def test_cannot_lift_twice(self):
        start = self.accepted - timedelta(days=2)
        self.service.register_suspension_event(
            "sup1", "supervisor", "EV-003", "台风", "typhoon", ["A区"], iso(start),
        )
        self.service.lift_suspension_event("sup1", "supervisor", "EV-003")
        with self.assertRaises(DomainError) as ctx:
            self.service.lift_suspension_event("sup1", "supervisor", "EV-003")
        self.assertEqual(409, ctx.exception.status)

    def test_lift_unknown_event_404(self):
        with self.assertRaises(DomainError) as ctx:
            self.service.lift_suspension_event("sup1", "supervisor", "NOPE")
        self.assertEqual(404, ctx.exception.status)

    def test_duplicate_event_no_rejected(self):
        start = self.accepted - timedelta(days=1)
        payload = ("sup1", "supervisor", "EV-004", "台风", "typhoon", ["A区"], iso(start))
        self.service.register_suspension_event(*payload)
        with self.assertRaises(DomainError) as ctx:
            self.service.register_suspension_event(*payload)
        self.assertEqual(409, ctx.exception.status)

    def test_other_region_unaffected(self):
        other = self._create_claim("CLM-S-002", "B区", lat=31.5)
        self._set_created_at(other["id"], self.accepted)
        start = self.accepted + timedelta(days=10)
        self.service.register_suspension_event(
            "sup1", "supervisor", "EV-005", "台风", "typhoon", ["A区"], iso(start),
        )
        snap_b = self._detail_deadline(other["id"])
        self.assertFalse(snap_b["suspended"])
        self.assertEqual(snap_b["paused_seconds"], 0)
        self.assertEqual(snap_b["current_deadline"], snap_b["original_deadline"])
        self.assertTrue(snap_b["overdue"])  # B 区案件 12 天受理后，再过 3 天即逾期

    def test_region_prefix_match(self):
        other = self._create_claim("CLM-S-003", "A区海曙街道", lat=30.2)
        self._set_created_at(other["id"], self.accepted)
        start = self.accepted + timedelta(days=10)
        self.service.register_suspension_event(
            "sup1", "supervisor", "EV-006", "台风", "typhoon", ["A区"], iso(start),
        )
        snap = self._detail_deadline(other["id"])
        self.assertTrue(snap["suspended"])

    def test_overlapping_events_via_service_no_double_count(self):
        self._set_created_at(self.claim["id"], self.accepted)
        d10 = self.accepted + timedelta(days=10)
        d13 = self.accepted + timedelta(days=13)
        d16 = self.accepted + timedelta(days=16)
        self.service.register_suspension_event(
            "sup1", "supervisor", "EV-101", "台风", "typhoon", ["A区"], iso(d10), iso(d13),
        )
        self.service.register_suspension_event(
            "sup1", "supervisor", "EV-102", "暴雨", "flood", ["A区"], iso(self.accepted + timedelta(days=12)), iso(d16),
        )
        snap = self._detail_deadline(self.claim["id"])
        self.assertEqual(len(snap["windows"]), 1)
        self.assertEqual(snap["paused_seconds"], 6 * 86400)
        self.assertEqual(snap["current_deadline"], iso(self.accepted + timedelta(days=21)))

    def test_queue_and_state_carry_deadline(self):
        start = self.accepted + timedelta(days=10)
        self._set_created_at(self.claim["id"], self.accepted)
        self.service.register_suspension_event(
            "sup1", "supervisor", "EV-007", "台风", "typhoon", ["A区"], iso(start),
        )
        queue = self.service.queue("supervisor", "sup1")
        row = next(c for c in queue if c["id"] == self.claim["id"])
        self.assertIn("deadline", row)
        self.assertTrue(row["deadline"]["suspended"])
        self.assertIn("original_deadline", row["deadline"])
        state = self.service.state("sup1", "supervisor")
        self.assertTrue(any(e["event_no"] == "EV-007" for e in state["events"]))
        actions = {t["action"] for t in state["timeline"]}
        self.assertIn("suspension.registered", actions)

    def test_events_list_requires_role(self):
        with self.assertRaises(DomainError) as ctx:
            self.service.list_suspension_events("viewer")
        self.assertEqual(403, ctx.exception.status)


class ReopenClaimServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = CatastropheClaimService(Path(self.tmp.name) / "test.db")
        self.claim = self.service.create_claim(
            "intake1", "intake", "CLM-R-001", "TY2026", "A区", "flood",
            "P-1", "R-1", 30.1, 121.1, 200000, False, False,
        )
        self.claim = self.service.triage_claim("sup1", "supervisor", self.claim["id"], self.claim["version"], 0.1)
        self.claim = self.service.assign_claim("sup1", "supervisor", self.claim["id"], "adj1", self.claim["version"])
        self.claim = self.service.record_survey("adj1", "adjuster", self.claim["id"], 0.3, "受损", "赔付", self.claim["version"])
        self.claim = self.service.submit_review("adj1", "adjuster", self.claim["id"], self.claim["version"])
        self.claim = self.service.finalize_claim("sup1", "supervisor", self.claim["id"], "approve", 100000, self.claim["version"])

    def tearDown(self):
        self.tmp.cleanup()

    def _reopen_and_process(self):
        claim = self.service.reopen_claim("sup1", "supervisor", self.claim["id"], "客户补充材料")
        self.assertEqual("reopened", claim["status"])
        # 重开后仍可看到原期限与暂停信息
        self.assertIn("deadline", claim)
        self.assertEqual(claim["deadline"]["deadline_days"], 15)
        claim = self.service.assign_claim("sup1", "supervisor", claim["id"], "adj2", claim["version"])
        self.service.add_evidence("adj2", "adjuster", claim["id"], "c" * 64, "new.pdf", "field")
        claim = self.service.record_survey("adj2", "adjuster", claim["id"], 0.4, "复查受损", "追加赔付", claim["version"])
        claim = self.service.submit_review("adj2", "adjuster", claim["id"], claim["version"])
        claim = self.service.finalize_claim("sup1", "supervisor", claim["id"], "approve", 120000, claim["version"])
        return claim

    def test_reopen_then_continue_to_finalize(self):
        claim = self._reopen_and_process()
        self.assertEqual("approved", claim["status"])
        self.assertEqual(120000, claim["final_payout"])
        detail = self.service.claim_detail("sup1", "supervisor", claim["id"])
        actions = [t["action"] for t in detail["timeline"]]
        self.assertIn("claim.reopened", actions)
        self.assertEqual(actions.count("claim.finalized"), 2)
        # 证据和查勘资料都保留，重开后可以继续补
        self.assertEqual(len(detail["evidence"]), 1)
        self.assertEqual(len(detail["survey_notes"]), 2)

    def test_reopen_requires_supervisor_and_reason(self):
        with self.assertRaises(DomainError) as ctx:
            self.service.reopen_claim("adj1", "adjuster", self.claim["id"], "x")
        self.assertEqual(403, ctx.exception.status)
        with self.assertRaises(DomainError):
            self.service.reopen_claim("sup1", "supervisor", self.claim["id"], "  ")

    def test_open_claim_cannot_reopen(self):
        fresh = self.service.create_claim(
            "intake1", "intake", "CLM-R-002", "TY2026", "A区", "flood",
            "P-2", "R-2", 30.3, 121.2, 100000, False, False,
        )
        with self.assertRaises(DomainError) as ctx:
            self.service.reopen_claim("sup1", "supervisor", fresh["id"], "x")
        self.assertEqual(409, ctx.exception.status)

    def test_reopened_claim_keeps_suspension_extensions(self):
        # 核定前登记一个已解除事件，重开后顺延仍然生效
        claim_id = self.claim["id"]
        base = datetime.now(timezone.utc) - timedelta(days=8)
        with self.service.connect() as conn:
            conn.execute("UPDATE claims SET created_at=? WHERE id=?", (iso(base), claim_id))
        self.service.register_suspension_event(
            "sup1", "supervisor", "EV-R-1", "台风", "typhoon", ["A区"],
            iso(base + timedelta(days=2)), iso(base + timedelta(days=5)),
        )
        self._reopen_and_process()
        snap = self.service.claim_detail("sup1", "supervisor", claim_id)["deadline"]
        self.assertEqual(snap["paused_seconds"], 3 * 86400)


if __name__ == "__main__":
    unittest.main()
