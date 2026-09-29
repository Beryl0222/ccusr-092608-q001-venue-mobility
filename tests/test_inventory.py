import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from venue_mobility.inventory import Inventory
from venue_mobility.errors import CapacityExhausted


class InventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.inv = Inventory()
        self.inv.apply_pool_snapshot({"pools": {
            "vehicle:shuttle": {"capacity": 3},
            "driver:shuttle": {"capacity": 2},
            "accessibility_seat:shuttle": {"capacity": 4},
        }})

    def test_atomic_check_rejects_whole_request_when_one_dimension_short(self) -> None:
        # 车辆够、司机不够 -> 整体拒绝，且没有任何维度被部分占用。
        with self.assertRaises(CapacityExhausted) as ctx:
            self.inv.check([("vehicle:shuttle", 3), ("driver:shuttle", 3)])
        self.assertEqual("driver:shuttle", ctx.exception.resource_ref)
        self.assertEqual(0, self.inv.held_of("vehicle:shuttle"))
        self.assertEqual(0, self.inv.held_of("driver:shuttle"))

    def test_commit_and_partial_release(self) -> None:
        self.inv.check([("vehicle:shuttle", 2), ("driver:shuttle", 2)])
        self.inv.commit("h1", "r1", "demand", "d1", "vehicle:shuttle", 2)
        self.inv.commit("h2", "r1", "demand", "d1", "driver:shuttle", 2)
        self.assertEqual(1, self.inv.available("vehicle:shuttle"))
        self.inv.release("vehicle:shuttle", 1, "demand", "d1")
        self.assertEqual(2, self.inv.available("vehicle:shuttle"))
        # 司机占用不受影响（维度独立）。
        self.assertEqual(0, self.inv.available("driver:shuttle"))

    def test_same_hold_id_replay_does_not_double_deduct(self) -> None:
        self.inv.commit("h1", "r1", "demand", "d1", "vehicle:shuttle", 2)
        self.inv.commit("h1", "r1", "demand", "d1", "vehicle:shuttle", 2)
        self.assertEqual(2, self.inv.held_of("vehicle:shuttle"))

    def test_distinct_hold_ids_accumulate_and_release_by_owner(self) -> None:
        self.inv.commit("h1", "r1", "demand", "d1", "vehicle:shuttle", 1)
        self.inv.commit("h2", "r9", "demand", "d9", "vehicle:shuttle", 1)
        self.inv.commit("h3", "re", "emergency", "rv1", "vehicle:shuttle", 1)
        self.assertEqual(3, self.inv.held_of("vehicle:shuttle"))
        # 释放 d1 的 1 辆，不能误释放 d9 或紧急占用。
        self.inv.release("vehicle:shuttle", 1, "demand", "d1")
        owners = sorted((h.owner_id, h.quantity) for h in self.inv.holds_of("demand", "d1"))
        self.assertEqual([], owners)
        self.assertEqual(1, sum(h.quantity for h in self.inv.holds_of("demand", "d9")))
        self.assertEqual(1, sum(h.quantity for h in self.inv.holds_of("emergency", "rv1")))

    def test_release_more_than_held_is_clamped_and_idempotent(self) -> None:
        self.inv.commit("h1", "r1", "demand", "d1", "driver:shuttle", 1)
        released = self.inv.release("driver:shuttle", 2, "demand", "d1")
        self.assertEqual(1, released)
        again = self.inv.release("driver:shuttle", 1, "demand", "d1")
        self.assertEqual(0, again)

    def test_unknown_resource_is_rejected(self) -> None:
        with self.assertRaises(CapacityExhausted):
            self.inv.available("vehicle:unknown")

    def test_non_positive_quantity_rejected(self) -> None:
        with self.assertRaises(CapacityExhausted):
            self.inv.check([("vehicle:shuttle", 0)])


if __name__ == "__main__":
    unittest.main()
