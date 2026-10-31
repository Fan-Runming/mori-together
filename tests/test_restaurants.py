import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, AsyncMock
from mori_together.core import Planner, Store
from mori_together.providers import Discovery, DemoEvents, GooglePlaces, ProviderError


class RestaurantTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now = datetime.now(timezone.utc)
        self.store = Store(':memory:')
        self.places = GooglePlaces(key='test-only')
        self.provider = Discovery(events=DemoEvents(), places=self.places)
        self.planner = Planner(self.store, self.provider, now=lambda: self.now)
        await self.planner.handle('a', 'new Alice')
        await self.planner.handle('b', 'join ' + self.store.current('a')['invite'] + ' Bob')
        self.pref = dict(city='Berkeley', start=(self.now+timedelta(days=1)).isoformat(),
                         end=(self.now+timedelta(days=1,hours=3)).isoformat(), budget=40, interest='restaurants', kind='restaurant')
        for actor in ('a','b'):
            await self.planner.handle(actor, 'preferences ' + json.dumps(self.pref))
        self.item = dict(id='fixture', name='Test restaurant', googleMapsUri='https://maps.google.com/?cid=fixture',
                         displayName={'text':'Test restaurant'}, businessStatus='OPERATIONAL',
                         formattedAddress='123 Test St, Berkeley, CA',
                         addressComponents=[{'types':['locality'],'longText':'Berkeley'}])

    async def request(self, method, url, **kwargs):
        return {'places':[self.item]} if url.endswith(':searchText') else dict(self.item)

    async def test_natural_cuisine_search_and_two_person_confirmation(self):
        self.planner.interpreter.interpret = AsyncMock(return_value={
            'action':'conversation','preferences':{'kind':'restaurant','interest':'hot pot'},'search':True})
        with patch('mori_together.providers.request_json', side_effect=self.request) as req:
            reply = await self.planner.handle('a', '想吃火锅')
            self.assertEqual(reply.card_kind,'carousel')
            self.assertIn('hot pot',req.call_args.kwargs['json']['textQuery'])
            self.assertNotIn('*', req.call_args.kwargs['headers']['X-Goog-FieldMask'])
            self.assertNotIn('Within shared budget',json.dumps(reply.card_payload))
            self.assertIn('Price unverified',json.dumps(reply.card_payload))
            self.assertEqual(self.store.current('b')['members']['b']['preferences']['interest'],'restaurants')
            await self.planner.handle('a','select google:fixture')
            v=self.store.current('a')['version']
            await self.planner.handle('a',f'confirm {v}')
            self.assertEqual(self.store.current('a')['state'],'proposed')
            result=await self.planner.handle('b',f'confirm {v}')
            self.assertEqual(result.card_kind,'detail')
            for action in ('calendar','event_link','calendar'):
                result=await self.planner.handle('a',json.dumps({'action':action}))
                self.assertIn('maps.google.com',result.text)
                self.assertIn('Not booked',result.text)
                self.assertIn('calendar.google.com',result.text)

    async def test_closed_business_clears_approval(self):
        with patch('mori_together.providers.request_json',side_effect=self.request):
            await self.planner.handle('a','search')
            await self.planner.handle('a','select google:fixture')
            v=self.store.current('a')['version']
            await self.planner.handle('a',f'confirm {v}')
            self.item['businessStatus']='CLOSED_PERMANENTLY'
            await self.planner.handle('b',f'confirm {v}')
            self.assertEqual(self.store.current('a')['approvals'],[])
            self.assertIsNone(self.store.current('a')['selected'])

    async def test_address_change_requires_new_approval(self):
        with patch('mori_together.providers.request_json',side_effect=self.request):
            await self.planner.handle('a','search')
            await self.planner.handle('a','select google:fixture')
            v=self.store.current('a')['version']
            await self.planner.handle('a',f'confirm {v}')
            self.item['formattedAddress']='456 New St, Berkeley, CA'
            await self.planner.handle('b',f'confirm {v}')
            self.assertEqual(self.store.current('a')['approvals'],[])
            self.assertGreater(self.store.current('a')['version'],v)

    async def test_conflicting_cuisines_do_not_search(self):
        for actor,food in [('a','hot pot'),('b','Japanese')]:
            await self.planner.handle(actor,'preferences '+json.dumps({'interest':food}))
        with patch('mori_together.providers.request_json',new_callable=AsyncMock) as req:
            result=await self.planner.handle('a','search')
            req.assert_not_called()
            self.assertIn('different cuisines',result.text)

    async def test_missing_key_is_explicit(self):
        with patch.dict('os.environ',{},clear=True):
            with self.assertRaisesRegex(ProviderError,'GOOGLE_PLACES_API_KEY'):
                await GooglePlaces().search(self.pref)

    async def test_unknown_prices_not_claimed_as_free_and_zero_budget_rejected(self):
        c=self.planner.constraints(self.store.current('a'))
        with patch('mori_together.providers.request_json',side_effect=self.request):
            events=await self.places.search(c)
        self.assertIn('Price unknown',self.planner.price_label(events[0]))
        self.assertFalse(self.planner.eligible(events[0],{**c,'budget':0}))
        self.assertFalse(self.planner.eligible({**events[0],'city':'Oakland'},c))

    async def test_demo_restaurants_are_explicit_fixtures(self):
        c=self.planner.constraints(self.store.current('a'))
        events=await DemoEvents().search(c)
        self.assertTrue(all(e['demo'] and e['kind']=='restaurant' for e in events))
        self.assertTrue(all(self.planner.eligible(e,c) for e in events))

    async def test_natural_park_search_with_zero_budget_is_not_free_claim(self):
        for actor in ('a', 'b'):
            await self.planner.handle(actor, 'preferences '+json.dumps({'kind':'place','interest':'parks','budget':0}))
        self.planner.interpreter.interpret = AsyncMock(return_value={
            'action':'conversation','preferences':{'kind':'place','interest':'scenic parks'},'search':True})
        with patch('mori_together.providers.request_json',side_effect=self.request) as req:
            result=await self.planner.handle('a','想去好看的公园散步')
            self.assertEqual(result.card_kind,'carousel')
            self.assertNotIn('includedType',req.call_args.kwargs['json'])
            self.assertIn('scenic parks',req.call_args.kwargs['json']['textQuery'])
            self.assertIn('Google Maps',result.card_payload['subtitle'])
            self.assertNotIn('Free',json.dumps(result.card_payload))
            self.assertIn('Price unknown',json.dumps(result.card_payload))
            await self.planner.handle('a','select google:fixture')
            v=self.store.current('a')['version']
            await self.planner.handle('a',f'confirm {v}')
            result=await self.planner.handle('b',f'confirm {v}')
            self.assertEqual(result.card_kind,'detail')
            self.assertIn('Suggested visit time',json.dumps(result.card_payload))

    async def test_unknown_locality_and_closed_place_are_not_recommended(self):
        for actor in ('a','b'):
            await self.planner.handle(actor,'preferences '+json.dumps({'kind':'place','interest':'parks'}))
        c=self.planner.constraints(self.store.current('a'))
        self.item['addressComponents']=[]
        event=self.places.normalize(self.item,c['start'],c['end'],'place')
        self.assertFalse(self.planner.eligible(event,c))
        self.item['addressComponents']=[{'types':['locality'],'longText':'Berkeley'}]
        self.item['businessStatus']='CLOSED_TEMPORARILY'
        event=self.places.normalize(self.item,c['start'],c['end'],'place')
        self.assertFalse(self.planner.eligible(event,c))


    async def test_broad_place_window_asks_before_suggesting_midnight(self):
        for actor in ('a', 'b'):
            await self.planner.handle(actor, 'preferences '+json.dumps({
                'kind':'place', 'interest':'parks',
                'end':(self.now+timedelta(days=2)).isoformat()}))
        with patch('mori_together.providers.request_json',new_callable=AsyncMock) as req:
            reply=await self.planner.execute('a',{'action':'conversation','language':'zh','search':True})
            self.assertIn('几点出发',reply.text)
            req.assert_not_called()

    async def test_chinese_place_reply_does_not_claim_verified_budget(self):
        with patch('mori_together.providers.request_json',side_effect=self.request):
            reply=await self.planner.execute('a',{'action':'conversation','language':'zh','search':True})
            self.assertEqual(reply.card_kind,'carousel')
            self.assertIn('费用和营业时间尚未核实',reply.text)
            self.assertNotIn('符合你们共同时间、地点和预算',reply.text)
