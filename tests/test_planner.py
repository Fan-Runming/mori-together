import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from mori_together.core import Planner, Store, Reply, principal, instant, PILOT_TZ
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

    async def test_direct_mention_commands_and_card_json_skip_llm(self):
        async def unexpected_llm(*args):
            raise AssertionError("Direct commands and card JSON must not use the LLM")
        self.p.interpreter.interpret = unexpected_llm
        await self.p.handle("a", '@mori-together preferences {"budget":25}')
        self.assertEqual(self.store.current("a")["members"]["a"]["preferences"]["budget"], 25)
        p = self.store.current("a")
        await self.p.handle("a", "@mori-together " + json.dumps({
            "action": "preferences", "plan_id": p["id"], "version": p["version"],
            **self.pref}))
        v = await self.proposed()
        await self.p.handle("a", f"@mori-together confirm {v}")
        self.assertEqual(self.store.current("a")["approvals"], ["a"])
        result = await self.p.handle("b", "@mori-together " + json.dumps({"action":"confirm", "version":v}))
        self.assertEqual(result.card_kind, "detail")

    async def test_natural_request_updates_only_speaker_and_searches(self):
        async def interpret(text, context):
            self.assertEqual(text, "最近有什么concert？预算20美元")
            self.assertNotIn("members", context)
            return {"action": "conversation", "language": "zh", "preferences": {"budget": 20, "interest": "music"}, "search": True}
        self.p.interpreter.interpret = interpret
        result = await self.p.handle("a", "@mori 最近有什么concert？预算20美元")
        self.assertEqual(result.card_kind, "carousel")
        plan = self.store.current("a")
        self.assertEqual(plan["members"]["a"]["preferences"]["budget"], 20)
        self.assertEqual(plan["members"]["b"]["preferences"]["budget"], 30)
        self.assertNotIn("select demo", result.text)

    async def test_natural_followup_resumes_pending_search(self):
        await self.p.handle("b", "forget")
        async def interpret(text, context):
            if text == "有什么好玩的":
                return {"action": "conversation", "language": "zh", "preferences": {}, "search": True}
            return {"action": "conversation", "preferences": self.pref, "search": False}
        self.p.interpreter.interpret = interpret
        result = await self.p.handle("a", "有什么好玩的")
        self.assertIsNone(result.card_kind)
        self.assertTrue(self.store.current("a")["search_requested"])
        result = await self.p.handle("b", "这是我的时间和预算")
        self.assertEqual(result.card_kind, "carousel")
        self.assertFalse(self.store.current("a")["search_requested"])

    async def test_ambiguous_natural_request_preserves_plan(self):
        await self.proposed()
        before = self.store.current("a")
        async def interpret(text, context):
            return {"action": "conversation", "question": "你说的是哪个周末？", "preferences": {}, "search": False}
        self.p.interpreter.interpret = interpret
        result = await self.p.handle("a", "那个周末呢")
        self.assertEqual(result.text, "你说的是哪个周末？")
        self.assertEqual(self.store.current("a"), before)

    async def test_complete_and_repeat_is_not_two_people(self):
        v = await self.proposed()
        await self.p.handle("a", f"confirm {v}")
        await self.p.handle("a", f"confirm {v}")
        self.assertEqual(self.store.current("a")["state"], "proposed")
        result = await self.p.handle("b", f"confirm {v}")
        self.assertEqual(result.card_kind, "detail")
        self.assertEqual(self.store.current("a")["state"], "confirmed")

    async def test_confirmation_card_and_followup_actions(self):
        from agent import wire_reply
        v = await self.proposed()
        blocked = await self.p.handle("a", json.dumps({"action": "calendar", "version": v}))
        self.assertIn("Both people", blocked.text)
        waiting = await self.p.handle("a", f"confirm {v}")
        self.assertIn("Waiting for Bob", waiting.text)
        result = await self.p.handle("b", f"confirm {v}")
        self.assertNotIn("https://", result.text)
        self.assertEqual(wire_reply(result).content[1].metadata["card_kind"], "detail")
        self.assertEqual((await self.p.handle("a", "status")).card_payload, result.card_payload)
        for cta, expected in zip(result.card_payload["ctas"], ["calendar.google.com", "View event and tickets"]):
            response = await self.p.handle("a", json.dumps(cta["selection"]))
            self.assertIn(expected, response.text)
            self.assertIn("[Open calendar draft](https://calendar.google.com/", response.text)
            self.assertIn("[View event and tickets](", response.text)
            self.assertIsNone(response.card_kind)
            self.assertEqual(self.store.current("a")["state"], "confirmed")
        await self.p.handle("b", 'preferences {"budget":10}')
        stale = await self.p.handle("a", json.dumps(result.card_payload["ctas"][0]["selection"]))
        self.assertIn("changed", stale.text)

    async def test_card_displays_event_local_date(self):
        await self.proposed()
        plan = self.store.current("a")
        plan["selected"]["start"] = "2026-11-02T04:00:00Z"
        plan["selected"]["end"] = None
        plan["state"] = "confirmed"
        plan["approvals"] = ["a", "b"]
        result = self.p.summary(plan)
        rows = {row["label"]: row["value"] for row in result.card_payload["summary_rows"]}
        self.assertEqual(rows["When"], "Sun, Nov 01 · 08:00 PM PST")
        self.assertIn("placeholder", rows["Calendar duration"])
        self.assertIn("20261102T040000Z%2F20261102T050000Z", self.p.calendar_url(plan["selected"]))

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

    async def test_reject_nan_budget_and_unreadable_time(self):
        self.assertIn("budget", (await self.p.handle("a", 'preferences {"budget":"NaN"}')).text)
        self.assertIn("2026-11-03 14:00", (await self.p.handle("a", 'preferences {"start":"tomorrow 2pm"}')).text)

    async def test_times_without_offset_are_pacific(self):
        day = (self.now + timedelta(days=2)).astimezone(PILOT_TZ).strftime("%Y-%m-%d")
        await self.p.handle("a", "preferences " + json.dumps({"start": f"{day} 2pm", "end": f"{day} 18:00", "budget": "$25"}))
        pref = self.store.current("a")["members"]["a"]["preferences"]
        expected = datetime.fromisoformat(f"{day}T14:00").replace(tzinfo=PILOT_TZ).astimezone(timezone.utc)
        self.assertEqual(instant(pref["start"]), expected)
        self.assertEqual(pref["budget"], 25)

    def test_invalid_or_ambiguous_local_times_are_not_silently_changed(self):
        for value in ("2026-11-03 13pm", "2026-11-03 0am", "2026-11-01 01:30", "2027-03-14 02:30"):
            with self.assertRaises(ValueError):
                instant(value)
        self.assertEqual(instant("2026-11-01T01:30:00-08:00").hour, 9)

    async def test_partner_update_does_not_void_preferences_card(self):
        card = (await self.p.handle("a", "status")).card_payload["submit_cta"]["selection"]
        await self.p.handle("b", 'preferences {"budget":20}')
        result = await self.p.handle("a", json.dumps({**card, "budget": 15}))
        self.assertIn("Saved", result.text)
        self.assertEqual(self.store.current("a")["members"]["a"]["preferences"]["budget"], 15)

    async def test_bare_confirm_shows_proposal_without_approving(self):
        await self.proposed()
        result = await self.p.handle("b", "confirm")
        self.assertIn("Not approved yet", result.text)
        self.assertEqual(result.card_kind, "review")
        self.assertEqual(self.store.current("a")["approvals"], [])

    async def test_internal_errors_are_not_shown(self):
        result = await self.p.handle("a", "preferences {not json")
        self.assertIn("type help", result.text)
        self.assertNotIn("Expecting", result.text)

    async def test_partner_activity_reported_once(self):
        v = await self.proposed()
        await self.p.handle("a", f"confirm {v}")
        first = (await self.p.handle("b", "status")).text
        self.assertIn("Alice proposed DEMO", first)
        self.assertIn("Alice said the proposal works", first)
        self.assertNotIn("Since your last message", (await self.p.handle("b", "status")).text)
        self.assertNotIn("• Alice", (await self.p.handle("a", "status")).text)

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
