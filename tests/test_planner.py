import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from mori_together.core import Planner, Store, Reply, principal
from mori_together.providers import DemoEvents, Ticketmaster, ProviderError


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now = datetime.now(timezone.utc)
        self.store = Store(":memory:")
        self.provider = DemoEvents()
        self.p = Planner(self.store, self.provider, now=lambda: self.now)
        self.pref = {"city": "Berkeley", "start": (self.now + timedelta(days=1)).isoformat(),
                     "end": (self.now + timedelta(days=1, hours=4)).isoformat(),
                     "budget": 30, "interest": "creative"}
        await self.p.handle("a", "new Alice")
        self.invite = self.store.current("a")["invite"]
        await self.p.handle("b", f"join {self.invite} Bob")
        for person in ("a", "b"):
            await self.p.handle(person, "preferences " + json.dumps(self.pref))

    async def proposed(self):
        await self.p.handle("a", "search")
        await self.p.handle("a", "select demo-0")
        return self.store.current("a")["version"]

    async def test_complete_and_repeat_is_not_two_people(self):
        v = await self.proposed()
        await self.p.handle("a", f"confirm {v}")
        await self.p.handle("a", f"confirm {v}")
        self.assertEqual(self.store.current("a")["state"], "proposed")
        result = await self.p.handle("b", f"confirm {v}")
        self.assertIn("calendar.google.com", result.text)
        self.assertEqual(self.store.current("a")["state"], "confirmed")

    async def test_preferences_invalidate_approval(self):
        v = await self.proposed()
        await self.p.handle("a", f"confirm {v}")
        await self.p.handle("b", 'preferences {"budget":10}')
        result = await self.p.handle("a", f"confirm {v}")
        self.assertIn("changed", result.text)
        self.assertEqual(self.store.current("a")["approvals"], [])
        self.assertIsNone(self.store.current("a")["selected"])

    async def test_actor_spoofing_ignored(self):
        v = await self.proposed()
        await self.p.handle("a", json.dumps({"action": "confirm", "version": v, "actor": "b"}))
        self.assertEqual(self.store.current("a")["approvals"], ["a"])

    async def test_invite_single_use_and_outsider_isolation(self):
        result = await self.p.handle("c", f"join {self.invite} Eve")
        self.assertIn("Invalid", result.text)
        self.assertIsNone(self.store.current("c"))
        self.assertNotIn("Alice:", (await self.p.handle("c", "status")).text)

    async def test_idempotent_delivery(self):
        await self.p.handle("a", 'preferences {"budget":22}', "msg-1")
        v = self.store.current("a")["version"]
        await self.p.handle("a", 'preferences {"budget":22}', "msg-1")
        self.assertEqual(self.store.current("a")["version"], v)

    async def test_cancellation_clears_approvals(self):
        v = await self.proposed()
        await self.p.handle("a", f"confirm {v}")
        async def canceled(event):
            return {**event, "status": "canceled"}
        self.provider.refresh = canceled
        result = await self.p.handle("b", f"confirm {v}")
        self.assertIn("no longer fits", result.text)
        self.assertEqual(self.store.current("a")["approvals"], [])

    async def test_price_change_requires_new_approval(self):
        v = await self.proposed()
        await self.p.handle("a", f"confirm {v}")
        async def changed(event):
            return {**event, "price_max": 20}
        self.provider.refresh = changed
        result = await self.p.handle("b", f"confirm {v}")
        self.assertIn("details changed", result.text)
        self.assertEqual(self.store.current("a")["approvals"], [])

    async def test_service_failure_preserves_plan(self):
        v = await self.proposed()
        before = self.store.current("a")
        async def fail(event):
            raise ProviderError("Service unavailable")
        self.provider.refresh = fail
        result = await self.p.handle("a", f"confirm {v}")
        self.assertIn("unavailable", result.text)
        self.assertEqual(before, self.store.current("a"))

    async def test_no_overlap(self):
        later = {"start": (self.now + timedelta(days=2)).isoformat(), "end": (self.now + timedelta(days=2,hours=3)).isoformat()}
        await self.p.handle("b", "preferences " + json.dumps(later))
        self.assertIn("overlapping", (await self.p.handle("a", "search")).text)

    async def test_reject_nan_budget_and_naive_time(self):
        self.assertIn("budget", (await self.p.handle("a", 'preferences {"budget":"NaN"}')).text)
        self.assertIn("timezone", (await self.p.handle("a", 'preferences {"start":"2026-11-03T12:00:00"}')).text)

    async def test_forget_erases_preferences_and_receipts(self):
        await self.p.handle("a", "status", "receipt")
        await self.p.handle("a", "forget")
        self.assertEqual(self.store.current("a")["members"]["a"]["preferences"], {})
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM receipts").fetchone()[0], 0)

    async def test_unknown_prices_excluded(self):
        c = self.p.constraints(self.store.current("a"))
        event = (await self.provider.search(c))[0]
        self.assertFalse(self.p.eligible({**event, "price_max": None}, c))
        self.assertFalse(self.p.eligible({**event, "city": "Oakland"}, c))

    async def test_delete_clears_both_sessions(self):
        self.assertIn("Only the creator", (await self.p.handle("b", "delete")).text)
        await self.p.handle("a", "delete")
        self.assertIsNone(self.store.current("a"))
        self.assertIsNone(self.store.current("b"))

    async def test_wire_cards(self):
        from agent import wire_reply
        r = await self.p.handle("a", "search")
        msg = wire_reply(r)
        self.assertEqual(msg.content[1].metadata["card_kind"], "carousel")
        self.assertLess(len(msg.content[1].metadata["card_payload"].encode()), 65536)
        self.assertTrue(wire_reply(Reply("hello")).msg_id)

    async def test_store_survives_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "state.db")
            first = Planner(Store(path), DemoEvents())
            await first.handle("a", "new Alice")
            first.store.db.close()
            second = Store(path)
            self.assertEqual(second.current("a")["members"]["a"]["name"], "Alice")
            second.db.close()

    async def test_no_silent_fixtures_in_live_mode(self):
        with patch.dict("os.environ", {"TICKETMASTER_API_KEY": ""}):
            with self.assertRaises(ProviderError):
                await Ticketmaster().search({})

    def test_principal_scoped_to_transport_session(self):
        self.assertNotEqual(principal("sender", "session1"), principal("sender", "session2"))


if __name__ == "__main__":
    unittest.main()
