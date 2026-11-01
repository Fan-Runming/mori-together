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

    async def test_restart_preserves_friend_and_membership_invalidates_approval(self):
        await self.pair()
        await self.planner.handle('b', json.dumps(self.pref([self.window(2, 9, 12)])))
        p = self.store.current('a')
        friend = p['members']['b']
        p['approvals'] = ['a']
        p['selected'] = {'id': 'old'}
        self.store.save(p)
        reply = await self.planner.handle('a', 'start all over to plan another one')
        after = self.store.current('a')
        self.assertEqual(after['id'], p['id'])
        self.assertEqual(after['members']['b'], friend)
        self.assertEqual(after['members']['a']['preferences'], {})
        self.assertEqual(after['approvals'], [])
        self.assertIsNone(after['selected'])
        self.assertGreater(after['version'], p['version'])
        self.assertEqual(reply.card_kind, 'form')
        self.assertNotIn('Type status', reply.text)

    async def test_new_existing_plan_opens_restart(self):
        await self.pair()
        reply = await self.planner.handle('a', 'create a plan')
        self.assertEqual(reply.card_kind, 'form')
        self.assertEqual(len(self.store.current('a')['members']), 2)

    async def test_match_friend_time_only_and_search(self):
        await self.pair()
        self.provider.search = AsyncMock(return_value=[])
        await self.planner.handle('a', json.dumps(self.pref([self.window(2, 9, 12)])))
        await self.planner.handle('b', json.dumps({**self.pref([self.window(3, 9, 12), self.window(4, 15, 18)]), 'name':'Wendy'}))
        before = self.store.current('a')
        self.provider.search.reset_mock()
        self.planner.interpreter.interpret.return_value = {'action':'match_time', 'name':'Wendy'}
        reply = await self.planner.handle('a', "previous preference but change the time to Wendy's time")
        after = self.store.current('a')
        self.assertEqual(after['members']['b'], before['members']['b'])
        self.assertEqual(after['members']['a']['preferences']['windows'], before['members']['b']['preferences']['windows'])
        for key in ('city','interest','budget'):
            self.assertEqual(after['members']['a']['preferences'][key], before['members']['a']['preferences'][key])
        self.assertEqual(self.provider.search.await_count, 2)
        self.assertIn('Wendy', reply.text)

    async def test_match_missing_or_unknown_friend_does_not_mutate(self):
        await self.pair()
        for name in ('Friend B', 'Unknown'):
            before = self.store.current('a')
            self.planner.interpreter.interpret.return_value = {'action':'match_time', 'name':name}
            await self.planner.handle('a', 'use their time')
            self.assertEqual(self.store.current('a'), before)

    async def test_shared_city_decision_updates_both_and_searches(self):
        await self.pair()
        self.provider.search = AsyncMock(return_value=[])
        await self.planner.handle('a', json.dumps(self.pref([self.window(2,9,12)])))
        await self.planner.handle('b', json.dumps({**self.pref([self.window(2,9,12)]), 'city':'San Francisco'}))
        before = self.store.current('a')
        before['selected'] = {'id':'old', 'title':'Previous proposal'}
        before['approvals'] = ['b']
        self.store.save(before)
        self.provider.search.reset_mock()
        self.planner.interpreter.interpret.return_value = {'action':'agree_city', 'city':'San fran'}
        reply = await self.planner.handle('a', 'we agreed on San fran')
        after = self.store.current('a')
        for actor in ('a','b'):
            self.assertEqual(after['members'][actor]['preferences'], {**before['members'][actor]['preferences'], 'city':'San Francisco'})
        self.assertEqual(after['approvals'], [])
        self.assertIsNone(after['selected'])
        self.provider.search.assert_awaited_once()
        self.assertEqual(self.provider.search.call_args.args[0]['city'], 'San Francisco')
        self.assertNotIn('You chose different cities', reply.text)
        self.assertIn('shared decision', reply.text)

    async def test_personal_city_change_does_not_update_friend(self):
        await self.pair()
        await self.planner.handle('b', json.dumps(self.pref([self.window(2,9,12)])))
        before = self.store.current('a')['members']['b']['preferences']
        self.planner.interpreter.interpret.return_value = {'action':'conversation','preferences':{'city':'San Francisco'}}
        await self.planner.handle('a', 'I prefer SF')
        self.assertEqual(self.store.current('a')['members']['b']['preferences'], before)

    async def test_invalid_shared_city_does_not_mutate(self):
        await self.pair()
        before = self.store.current('a')
        self.planner.interpreter.interpret.return_value = {'action':'agree_city','city':'New York'}
        await self.planner.handle('a', 'we agreed on New York')
        self.assertEqual(self.store.current('a'), before)

    async def test_shared_decisions_across_all_planning_fields(self):
        await self.pair()
        self.provider.search = AsyncMock(return_value=[])
        await self.planner.handle('a', json.dumps(self.pref([self.window(2,9,12)])))
        await self.planner.handle('b', json.dumps({**self.pref([self.window(2,9,12)]), 'kind':'restaurant','interest':'Japanese'}))
        # Changing the outing type clears the old event interest, but keeps a cuisine
        # that is already relevant for the partner.
        self.planner.interpreter.interpret.return_value = {'action':'agree_preferences','preferences':{'kind':'restaurant'}}
        await self.planner.handle('a', 'agreed on restaurant')
        p = self.store.current('a')
        self.assertEqual(p['members']['a']['preferences']['interest'], 'restaurants')
        self.assertEqual(p['members']['b']['preferences']['interest'], 'Japanese')
        self.assertEqual(self.provider.search.call_args.args[0]['kind'], 'restaurant')
        self.assertEqual(self.provider.search.call_args.args[0]['interest'], 'japanese')
        for updates in ({'kind':'restaurant','interest':'Chinese'}, {'budget':25},
                        {'windows':[self.window(3,15,18),self.window(4,15,18)]},
                        {'city':'San Francisco','kind':'place','interest':'museums','budget':35}):
            self.provider.search.reset_mock()
            self.planner.interpreter.interpret.return_value = {'action':'agree_preferences','preferences':updates}
            reply = await self.planner.handle('a', 'we agreed on these details')
            p = self.store.current('a')
            for key in updates:
                self.assertTrue(self.planner.same_preference(key, p['members']['a']['preferences'][key], updates[key]))
                self.assertEqual(p['members']['a']['preferences'][key],p['members']['b']['preferences'][key])
            self.assertTrue(self.provider.search.await_count)
            self.assertEqual(p['approvals'], [])
            self.assertNotIn('You chose different', reply.text)

    async def test_shared_update_is_atomic_and_survives_search_failure(self):
        await self.pair()
        self.provider.search = AsyncMock(return_value=[])
        for actor in ('a','b'):
            await self.planner.handle(actor,json.dumps(self.pref([self.window(2,9,12)])))
        before = self.store.current('a')
        self.planner.interpreter.interpret.return_value = {'action':'agree_preferences','preferences':{'city':'San Francisco','budget':-1}}
        await self.planner.handle('a','we agreed on invalid details')
        self.assertEqual(self.store.current('a'),before)
        self.provider.search.side_effect = ProviderError('offline')
        self.planner.interpreter.interpret.return_value = {'action':'agree_preferences','preferences':{'budget':20}}
        reply = await self.planner.handle('a','we agreed 20 per person')
        for member in self.store.current('a')['members'].values():
            self.assertEqual(member['preferences']['budget'],20)
        self.assertIn('temporarily unavailable',reply.text)

    async def test_shared_change_cannot_modify_partners_other_plan(self):
        await self.pair()
        old = self.store.current('a')
        await self.planner.handle('c','create a plan')
        other = self.store.current('c')
        other['members']['b'] = {'name':'Wendy','preferences':{'budget':99}}
        self.store.save(other)
        self.store.bind('b',other)
        self.store.db.commit()
        self.planner.interpreter.interpret.return_value = {'action':'agree_preferences','preferences':{'budget':20}}
        await self.planner.handle('a','we agreed 20')
        self.assertEqual(self.store.current('b')['members']['b']['preferences']['budget'],99)
        self.assertEqual(self.store.current('a'),old)

    async def test_legacy_food_preferences_route_to_restaurants(self):
        await self.pair()
        p=self.store.current('a')
        for m in p['members'].values():
            m['preferences']={k:v for k,v in self.pref([self.window(2,9,12)]).items() if k!='action'}
            m['preferences'].update(kind='event',interest='Chinese food')
        self.store.save(p)
        self.provider.search=AsyncMock(return_value=[])
        self.planner.interpreter.interpret.return_value={'action':'conversation','preferences':{'budget':50},'question':'Your budget is now $50. What new times work for you?'}
        reply=await self.planner.handle('a','i can adapt my time no worries, budge add to 50')
        p=self.store.current('a')
        self.assertEqual(p['members']['a']['preferences']['budget'],50)
        self.assertEqual(p['members']['b']['preferences']['budget'],40)
        self.assertEqual(self.planner.constraints(p)['kind'],'restaurant')
        self.assertIn('$50',reply.text)
        self.provider.search.assert_not_awaited()
        await self.planner.handle('a','search')
        self.assertEqual(self.provider.search.call_args.args[0]['kind'],'restaurant')

    async def test_interpreter_retries_invalid_multi_action_response(self):
        from unittest.mock import patch
        from mori_together.providers import ASIInterpreter
        def response(value):
            return {'choices':[{'message':{'content':json.dumps(value)}}]}
        valid={'action':'conversation','preferences':{'budget':50},'question':'Which times?'}
        with patch.dict('os.environ',{'ASI_ONE_API_KEY':'test'}), patch('mori_together.providers.request_json',new_callable=AsyncMock) as request:
            request.side_effect=[response([{'action':'edit'}]), response(valid)]
            result=await ASIInterpreter().interpret('budge add to 50, time flexible',{})
            self.assertEqual(result,valid)
            self.assertEqual(request.await_count,2)
            request.side_effect=[response({'action':'unknown'}),response({'action':'unknown'})]
            result=await ASIInterpreter().interpret('change things',{})
            self.assertEqual(result['action'],'conversation')
            self.assertNotIn('preferences',result)
            self.assertNotIn('type help',result['question'])
