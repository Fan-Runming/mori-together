import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, AsyncMock
from mori_together.core import Planner, Store
from mori_together.providers import Discovery, DemoEvents, YelpRestaurants, ProviderError


class RestaurantTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now = datetime.now(timezone.utc)
        self.store = Store(':memory:')
        self.yelp = YelpRestaurants(key='test-only')
        self.provider = Discovery(events=DemoEvents(), restaurants=self.yelp)
        self.planner = Planner(self.store, self.provider, now=lambda: self.now)
        await self.planner.handle('a', 'new Alice')
        await self.planner.handle('b', 'join ' + self.store.current('a')['invite'] + ' Bob')
        self.pref = dict(city='Berkeley', start=(self.now+timedelta(days=1)).isoformat(),
                         end=(self.now+timedelta(days=1,hours=3)).isoformat(), budget=40, interest='restaurants', kind='restaurant')
        for actor in ('a','b'):
            await self.planner.handle(actor, 'preferences ' + json.dumps(self.pref))
        self.item = dict(id='fixture', name='Test restaurant', url='https://www.yelp.com/biz/fixture',
                         image_url='https://example.com/cover.jpg', price='$$', is_closed=False,
                         location={'city':'Berkeley', 'display_address':['123 Test St','Berkeley, CA']})

    async def request(self, method, url, **kwargs):
        return {'businesses':[self.item]} if url.endswith('/search') else dict(self.item)

    async def test_natural_cuisine_search_and_two_person_confirmation(self):
        self.planner.interpreter.interpret = AsyncMock(return_value={
            'action':'conversation','preferences':{'kind':'restaurant','interest':'hot pot'},'search':True})
        with patch('mori_together.providers.request_json', side_effect=self.request) as req:
            reply = await self.planner.handle('a', '想吃火锅')
            self.assertEqual(reply.card_kind,'carousel')
            self.assertEqual(req.call_args.kwargs['params']['term'],'hot pot')
            self.assertNotIn('Within shared budget',json.dumps(reply.card_payload))
            self.assertIn('Price unverified',json.dumps(reply.card_payload))
            self.assertEqual(self.store.current('b')['members']['b']['preferences']['interest'],'restaurants')
            await self.planner.handle('a','select yelp:fixture')
            v=self.store.current('a')['version']
            await self.planner.handle('a',f'confirm {v}')
            self.assertEqual(self.store.current('a')['state'],'proposed')
            result=await self.planner.handle('b',f'confirm {v}')
            self.assertEqual(result.card_kind,'detail')
            for action in ('calendar','event_link','calendar'):
                result=await self.planner.handle('a',json.dumps({'action':action}))
                self.assertIn('maps/search',result.text)
                self.assertIn('Not reserved',result.text)
                self.assertIn('calendar.google.com',result.text)

    async def test_closed_business_clears_approval(self):
        with patch('mori_together.providers.request_json',side_effect=self.request):
            await self.planner.handle('a','search')
            await self.planner.handle('a','select yelp:fixture')
            v=self.store.current('a')['version']
            await self.planner.handle('a',f'confirm {v}')
            self.item['is_closed']=True
            await self.planner.handle('b',f'confirm {v}')
            self.assertEqual(self.store.current('a')['approvals'],[])
            self.assertIsNone(self.store.current('a')['selected'])

    async def test_price_change_requires_new_approval(self):
        with patch('mori_together.providers.request_json',side_effect=self.request):
            await self.planner.handle('a','search')
            await self.planner.handle('a','select yelp:fixture')
            v=self.store.current('a')['version']
            await self.planner.handle('a',f'confirm {v}')
            self.item['price']='$$$'
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
            with self.assertRaisesRegex(ProviderError,'YELP_API_KEY'):
                await YelpRestaurants().search(self.pref)

    async def test_unknown_prices_not_claimed_as_free_and_zero_budget_rejected(self):
        self.item.pop('price')
        c=self.planner.constraints(self.store.current('a'))
        with patch('mori_together.providers.request_json',side_effect=self.request):
            events=await self.yelp.search(c)
        self.assertIn('Price unknown',self.planner.price_label(events[0]))
        self.assertFalse(self.planner.eligible(events[0],{**c,'budget':0}))
        self.assertFalse(self.planner.eligible({**events[0],'city':'Oakland'},c))

    async def test_demo_restaurants_are_explicit_fixtures(self):
        c=self.planner.constraints(self.store.current('a'))
        events=await DemoEvents().search(c)
        self.assertTrue(all(e['demo'] and e['kind']=='restaurant' for e in events))
        self.assertTrue(all(self.planner.eligible(e,c) for e in events))
