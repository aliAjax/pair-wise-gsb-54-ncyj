import unittest

from src.formation import VESSEL, CREW, SPARE_BATCH, overlaps, parse_window, plan_formation, required_spare
from src.domain import ValidationError


FAULT_PAYLOAD = {"required_spare_km": 10.0}


def fault(fid, spare=10.0):
    return {"id": fid, "payload": {"required_spare_km": spare}}


def vessel(rid, code):
    return {"id": rid, "rtype": VESSEL, "code": code, "name": code}


def crew(rid, code):
    return {"id": rid, "rtype": CREW, "code": code, "name": code}


def batch(rid, code, km):
    return {"id": rid, "code": code, "name": code, "available_km": km}


def alloc(alloc_type, resource_id, start, end, spare=0.0):
    return {"alloc_type": alloc_type, "resource_id": resource_id,
            "window_start": start, "window_end": end, "spare_km": spare}


W0 = "2026-10-05T02:00:00+00:00"
W1 = "2026-10-06T10:00:00+00:00"
W_LATER = "2026-10-07T10:00:00+00:00"


class WindowTest(unittest.TestCase):
    def test_half_open_overlap(self):
        self.assertTrue(overlaps(W0, W1, "2026-10-06T09:00:00+00:00", W_LATER))
        # 首尾相接不算撞车，也不留空档
        self.assertFalse(overlaps(W0, W1, W1, W_LATER))
        self.assertFalse(overlaps(W0, W1, "2026-10-04T00:00:00+00:00", W0))

    def test_parse_supports_z_suffix(self):
        start, end = parse_window({"window_start": "2026-10-05T02:00:00Z", "window_end": "2026-10-06T10:00:00Z"})
        self.assertEqual(start, W0)

    def test_rejects_inverted_window(self):
        with self.assertRaises(ValidationError):
            parse_window({"window_start": W1, "window_end": W0})


class PlanFormationTest(unittest.TestCase):
    def test_proposed_when_all_resources_available(self):
        plan = plan_formation([fault(1)], W0, W1,
                              [vessel(1, "CS-1")], [crew(2, "SP-1")], [batch(3, "B-1", 50.0)], [])
        self.assertEqual(plan["state"], "proposed")
        self.assertEqual(plan["spare_required_km"], 10.0)
        self.assertEqual(plan["gaps"], [])
        self.assertEqual(plan["assignments"]["vessel_id"], 1)
        self.assertEqual(plan["assignments"]["crew_id"], 2)
        self.assertEqual(plan["assignments"]["batch_id"], 3)

    def test_spare_requirement_sums_faults(self):
        self.assertEqual(required_spare([fault(1, 10.0), fault(2, 15.75)]), 25.75)

    def test_standby_lists_every_gap_when_nothing_registered(self):
        plan = plan_formation([fault(1)], W0, W1, [], [], [], [])
        self.assertEqual(plan["state"], "standby")
        gap_types = {item["gap_type"] for item in plan["gaps"]}
        self.assertEqual(gap_types, {VESSEL, CREW, SPARE_BATCH})

    def test_vessel_busy_in_window_becomes_gap_but_free_later_ok(self):
        busy = alloc(VESSEL, 1, W0, W1)
        plan = plan_formation([fault(1)], W0, W1,
                              [vessel(1, "CS-1")], [crew(2, "SP-1")], [batch(3, "B-1", 50.0)], [busy])
        self.assertEqual(plan["state"], "standby")
        self.assertEqual(plan["gaps"][0]["gap_type"], VESSEL)
        # 换空闲窗口即可齐套
        plan2 = plan_formation([fault(1)], W1, W_LATER,
                               [vessel(1, "CS-1")], [crew(2, "SP-1")], [batch(3, "B-1", 50.0)], [busy])
        self.assertEqual(plan2["state"], "proposed")

    def test_batch_too_small_is_gap_but_other_batch_can_serve(self):
        plan = plan_formation([fault(1, 40.0)], W0, W1,
                              [vessel(1, "CS-1")], [crew(2, "SP-1")],
                              [batch(3, "B-SMALL", 10.0), batch(4, "B-BIG", 80.0)], [])
        self.assertEqual(plan["state"], "proposed")
        self.assertEqual(plan["assignments"]["batch_id"], 4)

        plan_small = plan_formation([fault(1, 40.0)], W0, W1,
                                    [vessel(1, "CS-1")], [crew(2, "SP-1")],
                                    [batch(3, "B-SMALL", 10.0)], [])
        self.assertEqual(plan_small["state"], "standby")
        self.assertEqual(plan_small["gaps"][0]["gap_type"], SPARE_BATCH)

    def test_busy_free_batch_skipped_for_smaller_free_one(self):
        # 大批次被窗口占用时，缺口按"没有空闲批次"报出
        busy = alloc(SPARE_BATCH, 4, W0, W1, 40.0)
        plan = plan_formation([fault(1, 40.0)], W0, W1,
                              [vessel(1, "CS-1")], [crew(2, "SP-1")],
                              [batch(3, "B-FREE", 5.0), batch(4, "B-BUSY", 100.0)], [busy])
        self.assertEqual(plan["state"], "standby")
        self.assertEqual(plan["gaps"][0]["gap_type"], SPARE_BATCH)
