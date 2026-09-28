import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import CatastropheClaimService, DomainError  # noqa: E402
import clocks  # noqa: E402


def iso(dt):
    return dt.astimezone(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")


class EventSuspensionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = CatastropheClaimService(Path(self.tmp.name) / "test.db")
        self.claim = self.service.create_claim(
            "intake1", "intake", "C-001", "TY-2026", "A区", "flood", "P-1", "R-1",
            30.1, 121.1, 500000, True, True,
        )
        # 回拨受理时间到 10 天前，模拟灾害发生时已在办的赔案
        accepted = datetime.now(timezone.utc) - timedelta(days=10)
        with self.service.connect() as conn:
            conn.execute("UPDATE claims SET created_at=? WHERE id=?", (iso(accepted), self.claim["id"]))
        self.claim = self.service.claim_detail("sup1", "supervisor", self.claim["id"])

    def tearDown(self):
        self.tmp.cleanup()

    def register(self, code="EV-1", region="A区", start=None, peril="typhoon"):
        start = start or (datetime.now(timezone.utc) - timedelta(days=2))
        return self.service.register_event(
            "sup1", "supervisor", code, "台风" + code, peril, region, iso(start),
        )

    def test_original_deadline_is_15_days_after_acceptance(self):
        view = self.claim["clock"]
        original = clocks.parse_ts(view["original_deadline"])
        created = clocks.parse_ts(self.claim["created_at"])
        self.assertEqual(timedelta(days=15), original - created)
        self.assertFalse(view["overdue"])

    def test_register_suspends_region_claims_and_blocks_processing(self):
        ev = self.register()
        self.assertIn(self.claim["id"], [x["claim_id"] for x in ev["suspended_claims"]])
        detail = self.service.claim_detail("sup1", "supervisor", self.claim["id"])
        self.assertEqual("suspended", detail["status"])
        self.assertTrue(detail["clock"]["clock_stopped"])
        with self.assertRaises(DomainError) as ctx:
            self.service.triage_claim("sup1", "supervisor", self.claim["id"], self.claim["version"])
        self.assertEqual(409, ctx.exception.status)
        # 其他区域案件不受影响
        other = self.service.create_claim(
            "intake1", "intake", "C-002", "TY-2026", "B区", "flood", "P-2", "R-2",
            31.1, 121.1, 100000,
        )
        self.assertEqual("received", other["status"])

    def test_lift_extends_by_actual_pause_and_reopens(self):
        start = datetime.now(timezone.utc) - timedelta(days=3)
        self.register(start=start)
        suspended = self.service.claim_detail("sup1", "supervisor", self.claim["id"])
        self.assertEqual("suspended", suspended["status"])

        end = start + timedelta(days=2)  # 实际暂停 2 天
        result = self.service.lift_event("sup1", "supervisor", 1, iso(end))
        reopened = result["reopened_claims"][0]
        self.assertAlmostEqual(2.0, reopened["paused_days"], places=2)
        detail = self.service.claim_detail("sup1", "supervisor", self.claim["id"])
        self.assertEqual("received", detail["status"], )
        expected = clocks.parse_ts(detail["clock"]["original_deadline"]) + timedelta(days=2)
        self.assertEqual(iso(expected), detail["clock"]["current_deadline"])
        self.assertFalse(detail["clock"]["clock_stopped"])

    def test_overlapping_events_do_not_double_count(self):
        start = datetime.now(timezone.utc) - timedelta(days=6)
        self.register(code="EV-A", start=start)
        # 与 EV-A 重叠 1 天的第二个事件（同一区域）
        self.register(code="EV-B", start=start + timedelta(days=2))
        # 第一个事件 4 天后解除
        self.service.lift_event("sup1", "supervisor", 1, iso(start + timedelta(days=4)))
        # 案件仍在 EV-B 中止中，不能重开
        detail = self.service.claim_detail("sup1", "supervisor", self.claim["id"])
        self.assertEqual("suspended", detail["status"])
        # 第二个事件 5 天后解除（两事件并集 = 5 天，不是 4+3=7 天）
        self.service.lift_event("sup1", "supervisor", 2, iso(start + timedelta(days=5)))
        detail = self.service.claim_detail("sup1", "supervisor", self.claim["id"])
        self.assertEqual("received", detail["status"])
        self.assertAlmostEqual(5.0, detail["clock"]["paused_days"], places=2)
        expected = clocks.parse_ts(detail["clock"]["original_deadline"]) + timedelta(days=5)
        self.assertEqual(iso(expected), detail["clock"]["current_deadline"])

    def test_claim_created_during_active_event_is_suspended_immediately(self):
        self.register()
        later = self.service.create_claim(
            "intake1", "intake", "C-010", "TY-2026", "A区", "flood", "P-10", "R-10",
            30.2, 121.2, 200000,
        )
        self.assertEqual("suspended", later["status"])
        self.assertTrue(later["clock"]["clock_stopped"])

    def test_manual_reopen_blocked_while_event_active(self):
        self.register()
        with self.assertRaises(DomainError) as ctx:
            self.service.reopen_claim("sup1", "supervisor", self.claim["id"])
        self.assertEqual(409, ctx.exception.status)

    def test_permissions_and_validation(self):
        with self.assertRaises(DomainError) as ctx:
            self.service.register_event("adj1", "adjuster", "EV-X", "x", "typhoon", "A区", iso(datetime.now(timezone.utc)))
        self.assertEqual(403, ctx.exception.status)
        with self.assertRaises(DomainError):
            self.register(peril="volcano")
        future = datetime.now(timezone.utc) + timedelta(days=1)
        with self.assertRaises(DomainError):
            self.register(code="EV-F", start=future)
        self.register(code="EV-DUP")
        with self.assertRaises(DomainError) as ctx:
            self.register(code="EV-DUP")
        self.assertEqual(409, ctx.exception.status)

    def test_lift_already_lifted_rejected(self):
        self.register()
        self.service.lift_event("sup1", "supervisor", 1)
        with self.assertRaises(DomainError) as ctx:
            self.service.lift_event("sup1", "supervisor", 1)
        self.assertEqual(409, ctx.exception.status)

    def test_completed_claim_keeps_original_deadline_and_can_still_process_after_lift(self):
        # 先完成全流程，再登记/解除事件：终态案件不被挂起、不判逾期
        raw = self.service.create_claim(
            "intake1", "intake", "C-100", "TY-2026", "A区", "flood", "P-100", "R-100",
            30.5, 121.5, 300000,
        )
        cid = raw["id"]
        c = self.service.triage_claim("sup1", "supervisor", cid, raw["version"], 0.1)
        c = self.service.assign_claim("sup1", "supervisor", cid, "adjuster1", c["version"])
        c = self.service.record_survey("adjuster1", "adjuster", cid, 0.5, "受损", "赔付", c["version"])
        c = self.service.submit_review("adjuster1", "adjuster", cid, c["version"])
        c = self.service.finalize_claim("sup1", "supervisor", cid, "approve", 100000, c["version"])
        self.assertEqual("approved", c["status"])
        ev = self.register()
        self.assertNotIn(cid, [x["claim_id"] for x in ev["suspended_claims"]])
        self.service.lift_event("sup1", "supervisor", ev["id"])
        detail = self.service.claim_detail("sup1", "supervisor", cid)
        self.assertEqual("approved", detail["status"])
        self.assertFalse(detail["clock"]["overdue"])


    def test_overdue_before_event_stays_overdue_while_clock_stopped(self):
        # 案件 20 天前受理（原期限 5 天前已过），今天才登记事件：逾期不被洗白
        old = datetime.now(timezone.utc) - timedelta(days=20)
        with self.service.connect() as conn:
            conn.execute("UPDATE claims SET created_at=? WHERE id=?", (iso(old), self.claim["id"]))
        self.register(start=datetime.now(timezone.utc) - timedelta(hours=2))
        detail = self.service.claim_detail("sup1", "supervisor", self.claim["id"])
        self.assertTrue(detail["clock"]["clock_stopped"])
        self.assertTrue(detail["clock"]["overdue"])


if __name__ == "__main__":
    unittest.main()
