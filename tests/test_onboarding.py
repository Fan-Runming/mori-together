import json
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock

from mori_together.core import Planner, Store, instant, PILOT_TZ
from mori_together.providers import DemoEvents, ProviderError


class OnboardingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Includes the Pacific DST transition on Nov 1.
        self.now = datetime(2026, 10, 31, 20, tzinfo=timezone.utc)
        self.store = Store(':memory:')
        self.provider = DemoEvents()
        self.planner = Planner(self.store, self.provider, now=lambda: self.now)
        self.planner.interpreter.interpret = AsyncMock()

    async def pair(self):
        await self.planner.handle('a', 'create a plan')
        await self.planner.handle('b', self.store.current('a')['invite'])

    def pref(self, windows):
        return dict(action='preferences', city='Berkeley', budget=40, interest='music', windows=windows)

    def window(self, day, a, b):
        return {'start': f'2026-11-{day:02}T{a:02}:00:00-08:00',
                'end': f'2026-11-{day:02}T{b:02}:00:00-08:00'}

    async def test_explicit_start_works_without_model_or_name(self):
        for actor, text in [('a', '@Mori Together create a plan'), ('b', '帮我们创建一个计划')]:
            reply = await self.planner.handle(actor, text)
            self.assertEqual(reply.card_kind, 'form')
            self.assertTrue(self.store.current(actor)['invite'])
        self.planner.interpreter.interpret.assert_not_called()

    async def test_first_outing_request_keeps_preferences(self):
        self.planner.interpreter.interpret.return_value = {
            'action': 'conversation', 'search': True,
            'preferences': {'interest': 'music', 'kind': 'event', 'budget': 30}}
        reply = await self.planner.handle('a', 'I want music this weekend under 30')
        self.assertEqual(reply.card_kind, 'form')
        self.assertEqual(self.store.current('a')['members']['a']['preferences']['budget'], 30)
        self.assertIn('music', reply.text)

    async def test_select_slots_and_search_automatically_once(self):
        await self.pair()
        self.provider.search = AsyncMock(return_value=[])
        slots = self.planner.slot_options()
        selected = [o for o in slots if instant(o['value'].split('|')[0]).astimezone(PILOT_TZ).date().isoformat() == '2026-11-02'][:2]
        cmd = dict(action='preferences', city='Berkeley', budget=30, interest='music',
                   slot_1=selected[0]['value'], slot_2=selected[1]['value'], slot_3='none')
        await self.planner.handle('a', json.dumps(cmd))
        self.provider.search.assert_not_awaited()
        reply = await self.planner.handle('b', json.dumps(cmd))
        self.assertEqual(self.provider.search.await_count, 2)
        self.assertNotIn('Type search', reply.text)
        self.assertEqual(len(self.store.current('a')['members']['a']['preferences']['windows']), 2)
        self.assertEqual(instant(selected[0]['value'].split('|')[0]).hour, 17)  # 9am PST

    async def test_only_real_intersections_are_eligible_and_searched(self):
        await self.pair()
        self.provider.search = AsyncMock(return_value=[])
        await self.planner.handle('a', json.dumps(self.pref([self.window(2, 9, 12), self.window(2, 18, 22)])))
        await self.planner.handle('b', json.dumps(self.pref([self.window(2, 10, 20)])))
        c = self.planner.constraints(self.store.current('a'))
        self.assertEqual([(instant(w['start']).hour, instant(w['end']).hour) for w in c['windows']], [(18,20), (2,4)])
        self.assertEqual(self.provider.search.await_count, 2)
        event = dict(kind='event', city='Berkeley', status='onsale', start='2026-11-02T14:00:00-08:00', end=None, price_max=10)
        self.assertFalse(self.planner.eligible(event, c))
        event['start'] = '2026-11-02T19:00:00-08:00'
        self.assertTrue(self.planner.eligible(event, c))

    async def test_invalid_slots_preserve_previous_preferences(self):
        await self.pair()
        await self.planner.handle('a', json.dumps(self.pref([self.window(2, 9, 12)])))
        before = self.store.current('a')
        reply = await self.planner.handle('a', json.dumps(self.pref([self.window(2, 12, 9)])))
        self.assertIn('end after start', reply.text)
        self.assertEqual(self.store.current('a'), before)

    async def test_search_failure_keeps_form_and_natural_preferences(self):
        await self.pair()
        self.provider.search = AsyncMock(side_effect=ProviderError('offline'))
        prefs = self.pref([self.window(2, 9, 12)])
        await self.planner.handle('a', json.dumps(prefs))
        await self.planner.handle('b', json.dumps(prefs))
        self.assertEqual(self.store.current('b')['members']['b']['preferences']['budget'], 40)
        self.planner.interpreter.interpret.return_value = {'action': 'conversation', 'preferences': {'budget': 25}}
        reply = await self.planner.handle('b', 'under 25')
        self.assertIn('saved', reply.text)
        self.assertEqual(self.store.current('b')['members']['b']['preferences']['budget'], 25)

    async def test_new_model_action_preserves_multiple_windows(self):
        self.planner.interpreter.interpret.return_value = {'action':'new', 'preferences':self.pref([self.window(2,9,12), self.window(3,18,22)])}
        await self.planner.handle('a', 'Help us arrange an outing, Monday morning or Tuesday evening')
        self.assertEqual(len(self.store.current('a')['members']['a']['preferences']['windows']), 2)

    async def test_form_has_clickable_slots_not_timestamp_text(self):
        reply = await self.planner.handle('a', 'create a plan')
        fields = reply.card_payload['fields']
        self.assertFalse(any(f['name'] in ('start', 'end') for f in fields))
        slots = [f for f in fields if f['name'].startswith('slot_')]
        self.assertEqual(len(slots), 3)
        self.assertTrue(all(f['kind'] == 'select' and f['options'] for f in slots))

    async def test_model_past_weekend_does_not_block_creation(self):
        self.planner.interpreter.interpret.return_value = {
            'action':'conversation', 'search':True, 'preferences':{
                'interest':'music', 'budget':30,
                'windows':[{'start':'2026-10-31T00:00:00-07:00','end':'2026-11-01T23:59:59-08:00'}]}}
        reply = await self.planner.handle('a', 'music this weekend under 30')
        self.assertEqual(reply.card_kind, 'form')
        prefs = self.store.current('a')['members']['a']['preferences']
        self.assertEqual(prefs, {'interest':'music', 'budget':30})

    async def test_restated_windows_use_instant_comparison(self):
        self.assertTrue(self.planner.same_preference('windows',
            [{'start':'2026-11-02T09:00:00-08:00','end':'2026-11-02T12:00:00-08:00'}],
            [{'start':'2026-11-02T17:00:00Z','end':'2026-11-02T20:00:00Z'}]))
