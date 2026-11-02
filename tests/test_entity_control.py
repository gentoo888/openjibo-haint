"""Everyday controls with real dispatch and HA registry/service doubles."""
import test_light_commands as fixtures
from test_light_commands import module, entities, devices
import unittest
import types
from unittest.mock import AsyncMock
from custom_components.jibo.entity_control import service_data

areas = module('homeassistant.helpers.area_registry')


class EntityControlTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixtures.LightTests.setUp(self)
        self.states = {}
        self.coordinator.hass.states = types.SimpleNamespace(get=self.states.get)
        areas.async_get = lambda hass: types.SimpleNamespace(areas={
            'bedroom': types.SimpleNamespace(id='bedroom', name='Bedroom', aliases={'Sleeping room'})})

    def add(self, entity_id, name, area='room', aliases=(), **attrs):
        self.registry.entities[entity_id] = types.SimpleNamespace(
            domain=entity_id.split('.')[0], entity_id=entity_id, area_id=area,
            device_id='device', aliases=aliases)
        state = types.SimpleNamespace(name=name, state='off', attributes=attrs)
        self.states[entity_id] = state
        return state

    async def command(self, domain, action, **kwargs):
        await self.coordinator._handle_command(dict(command='entity_control', requestId='r',
            domain=domain, action=action, scope='room', **kwargs), authenticated=True)

    async def test_actions_exact_calls(self):
        cases = [
            ('light', 'color', 'blue', {'supported_color_modes': ['rgb']}, 'turn_on', {'color_name': 'blue'}),
            ('light', 'brightness', '40', {'supported_color_modes': ['brightness']}, 'turn_on', {'brightness_pct': 40}),
            ('light', 'color_temperature', '3000', {'supported_color_modes': ['color_temp']}, 'turn_on', {'color_temp_kelvin': 3000}),
            ('switch', 'turn_off', None, {}, 'turn_off', {}),
            ('fan', 'percentage', '50', {'supported_features': 1}, 'set_percentage', {'percentage': 50}),
            ('cover', 'position', '25', {'supported_features': 4}, 'set_cover_position', {'position': 25}),
            ('cover', 'open', None, {'supported_features': 1}, 'open_cover', {}),
            ('cover', 'close', None, {'supported_features': 2}, 'close_cover', {}),
            ('cover', 'stop', None, {'supported_features': 8}, 'stop_cover', {}),
            ('climate', 'hvac_mode', 'heat', {'hvac_modes': ['heat', 'off']}, 'set_hvac_mode', {'hvac_mode': 'heat'}),
        ]
        for domain, action, value, attrs, service, data in cases:
            with self.subTest(domain=domain, action=action):
                self.registry.entities.clear()
                self.coordinator.hass.services.async_call.reset_mock()
                self.add(domain + '.test', 'Test', **attrs)
                await self.command(domain, action, value=value)
                self.coordinator.hass.services.async_call.assert_awaited_once_with(
                    domain, service, {'entity_id': domain + '.test', **data}, blocking=True)

    async def test_area_and_device_inheritance(self):
        self.add('light.a', 'A', area='bedroom')
        self.add('light.b', 'B', area=None)
        devices.async_get = lambda hass: types.SimpleNamespace(async_get=lambda _: types.SimpleNamespace(area_id='bedroom'))
        await self.coordinator._handle_command(dict(command='entity_control', requestId='r', domain='light',
            action='turn_on', scope='area', targetName='Sleeping room'), authenticated=True)
        self.assertEqual(self.coordinator.hass.services.async_call.await_count, 2)

    async def test_alias_and_duplicate_names(self):
        self.add('fan.a', 'Ceiling fan', aliases={'breeze'})
        await self.coordinator._handle_command(dict(command='entity_control', requestId='r', domain='fan',
            action='turn_on', scope='named', targetName='breeze'), authenticated=True)
        self.coordinator.hass.services.async_call.assert_awaited_once()
        self.add('fan.b', 'Ceiling fan', aliases={'breeze'})
        self.coordinator.hass.services.async_call.reset_mock()
        await self.coordinator._handle_command(dict(command='entity_control', requestId='r', domain='fan',
            action='turn_on', scope='named', targetName='breeze'), authenticated=True)
        self.coordinator.hass.services.async_call.assert_not_awaited()
        self.assertEqual(self.coordinator._send_command_result.call_args.args[1], 'needs_clarification')

    async def test_group_preflight(self):
        self.add('light.a', 'A', supported_color_modes=['rgb'])
        self.add('light.b', 'B', supported_color_modes=['onoff'])
        await self.command('light', 'color', value='blue')
        self.coordinator.hass.services.async_call.assert_not_awaited()
        self.assertEqual(self.coordinator._send_command_result.call_args.kwargs['message'], 'unsupported')

    async def test_invalid_and_unavailable(self):
        state = self.add('fan.a', 'Fan', supported_features=1)
        for value in ['-1', '101', 'nan', 'inf', 'bad']:
            await self.command('fan', 'percentage', value=value)
            self.assertEqual(self.coordinator._send_command_result.call_args.kwargs['message'], 'invalid_value')
        state.state = 'unavailable'
        await self.command('fan', 'turn_on')
        self.assertEqual(self.coordinator._send_command_result.call_args.kwargs['message'], 'unavailable')
        self.coordinator.hass.services.async_call.assert_not_awaited()

    async def test_scene_script_named_and_allowlist(self):
        for domain in ['scene', 'script']:
            self.add(domain + '.bedtime', 'Bedtime')
            await self.coordinator._handle_command(dict(command='entity_control', requestId='r', domain=domain,
                action='activate', scope='named', targetName='Bedtime'), authenticated=True)
        self.assertEqual(self.coordinator.hass.services.async_call.await_count, 2)
        self.coordinator.hass.services.async_call.reset_mock()
        await self.command('lock', 'unlock')
        self.coordinator.hass.services.async_call.assert_not_awaited()

    async def test_explicit_entity_revalidates_domain(self):
        self.add('switch.a', 'A')
        await self.coordinator._handle_command(dict(command='entity_control', requestId='r', domain='fan',
            action='turn_on', scope='entity', entityId='switch.a'), authenticated=True)
        self.coordinator.hass.services.async_call.assert_not_awaited()

    def test_temperature_conversion_and_bounds(self):
        state = types.SimpleNamespace(state='heat', attributes=dict(supported_features=1,
            temperature_unit='°F', min_temp=45, max_temp=90))
        service, data = service_data('climate', 'temperature', dict(value='20', unit='celsius'), state)
        self.assertEqual((service, data), ('set_temperature', {'temperature': 68}))
        with self.assertRaisesRegex(ValueError, 'invalid_value'):
            service_data('climate', 'temperature', dict(value='200'), state)

    async def test_new_command_authentication(self):
        self.add('fan.a', 'Fan')
        self.coordinator._verify_command_auth = lambda payload: False
        await self.coordinator._handle_command(dict(command='entity_control', requestId='r', domain='fan',
            action='turn_on', scope='room'))
        self.coordinator.hass.services.async_call.assert_not_awaited()
        self.coordinator._send_command_result.assert_awaited_once_with('r', 'error', message='auth_failed')

    async def test_area_fallback_for_single_device_and_clarification_selection(self):
        self.add('fan.a', 'Desk fan', area='bedroom')
        payload = dict(command='entity_control', requestId='r', domain='fan', action='turn_on',
            scope='named', targetName='bedroom')
        await self.coordinator._handle_command(payload, authenticated=True)
        self.coordinator.hass.services.async_call.assert_awaited_once()
        self.add('fan.b', 'Ceiling fan', area='bedroom')
        self.coordinator.hass.services.async_call.reset_mock()
        await self.coordinator._handle_command(payload, authenticated=True)
        self.coordinator.hass.services.async_call.assert_not_awaited()
        self.assertEqual(self.coordinator._send_command_result.call_args.args[1], 'needs_clarification')
        payload.update(scope='entity', entityId='fan.a')
        await self.coordinator._handle_command(payload, authenticated=True)
        self.coordinator.hass.services.async_call.assert_awaited_once_with(
            'fan', 'turn_on', {'entity_id': 'fan.a'}, blocking=True)

    async def test_legacy_named_light_alias_and_ambiguity(self):
        self.add('light.a', 'A', aliases={'bedside'})
        self.add('light.b', 'B', aliases={'bedside'})
        await self.coordinator._handle_command(dict(command='lights_on_named', requestId='r', targetName='bedside'), authenticated=True)
        self.coordinator.hass.services.async_call.assert_not_awaited()
        self.assertEqual(self.coordinator._send_command_result.call_args.args[1], 'needs_clarification')

    async def test_whole_home_requires_explicit_scope(self):
        self.add('switch.a', 'A')
        self.add('switch.b', 'B', area='bedroom')
        await self.command('switch', 'turn_off')
        self.assertEqual(self.coordinator.hass.services.async_call.await_count, 1)
        self.coordinator.hass.services.async_call.reset_mock()
        await self.coordinator._handle_command(dict(command='entity_control', requestId='r', domain='switch',
            action='turn_off', scope='all'), authenticated=True)
        self.assertEqual(self.coordinator.hass.services.async_call.await_count, 2)

    async def test_partial_service_failure_is_not_success(self):
        self.add('switch.a', 'A')
        self.add('switch.b', 'B')
        self.coordinator.hass.services.async_call.side_effect = [None, RuntimeError('device failed')]
        await self.command('switch', 'turn_on')
        self.assertEqual(self.coordinator._send_command_result.call_args.args[1], 'error')

    def test_temperature_system_unit_and_warmth_bounds(self):
        state = types.SimpleNamespace(state='heat', attributes=dict(supported_features=1, min_temp=7, max_temp=35))
        _, data = service_data('climate', 'temperature', dict(value='68', unit='fahrenheit'), state, '°C')
        self.assertEqual(data['temperature'], 20)
        state.attributes = dict(supported_color_modes=['color_temp'], min_color_temp_kelvin=3000, max_color_temp_kelvin=5000)
        self.assertEqual(service_data('light', 'color_temperature', dict(value='warm white'), state)[1], {'color_temp_kelvin': 3000})
        with self.assertRaisesRegex(ValueError, 'invalid_value'):
            service_data('light', 'color_temperature', dict(value='6000'), state)

    async def test_scene_never_activated_is_still_callable(self):
        self.add('scene.movie', 'Movie').state = 'unknown'
        await self.coordinator._handle_command(dict(command='entity_control', requestId='r', domain='scene',
            action='activate', scope='named', targetName='Movie'), authenticated=True)
        self.coordinator.hass.services.async_call.assert_awaited_once_with(
            'scene', 'turn_on', {'entity_id': 'scene.movie'}, blocking=True)

    async def test_missing_area_and_target(self):
        self.coordinator._get_jibo_area_id = lambda: None
        await self.command('fan', 'turn_on')
        self.assertEqual(self.coordinator._send_command_result.call_args.kwargs['message'], 'missing_area')
        await self.coordinator._handle_command(dict(command='entity_control', requestId='r', domain='fan',
            action='turn_on', scope='named', targetName='missing'), authenticated=True)
        self.assertEqual(self.coordinator._send_command_result.call_args.args[1], 'not_found')
        self.coordinator.hass.services.async_call.assert_not_awaited()


class ConversationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixtures.LightTests.setUp(self)
        self.call = self.coordinator.hass.services.async_call

    async def converse(self, text, response=None):
        self.call.return_value = {'response': response} if response else None
        await self.coordinator._handle_command(dict(command='conversation', requestId='r', text=text), authenticated=True)
        return self.coordinator._send_command_result.await_args

    async def test_assist_answer_is_spoken(self):
        result = await self.converse('lock the front door', {'response_type': 'action_done',
            'speech': {'plain': {'speech': 'Locked the front door'}}})
        self.call.assert_awaited_once_with('conversation', 'process',
            {'text': 'lock the front door', 'agent_id': 'conversation.home_assistant'}, blocking=True, return_response=True)
        self.assertEqual(result.args, ('r', 'ok')); self.assertEqual(result.kwargs, {'speech': 'Locked the front door'})

    async def test_unmatched_sentence_lets_jibo_carry_on(self):
        result = await self.converse('who wrote hamlet', {'response_type': 'error', 'data': {'code': 'no_intent_match'},
            'speech': {'plain': {'speech': "Sorry, I couldn't understand that"}}})
        self.assertEqual(result.args, ('r', 'not_found'))

    async def test_assist_errors_are_never_spoken(self):
        result = await self.converse('tell me a story', {'response_type': 'error', 'data': {'code': 'no_valid_targets'},
            'speech': {'plain': {'speech': 'Sorry, I am not aware of any device called a story'}}})
        self.assertEqual(result.args, ('r', 'not_found'))

    async def test_query_answer_is_spoken(self):
        result = await self.converse('is the front door locked', {'response_type': 'query_answer',
            'speech': {'plain': {'speech': 'Yes, Front Door is locked'}}})
        self.assertEqual(result.kwargs, {'speech': 'Yes, Front Door is locked'})

    async def test_bad_text_is_rejected_without_calling_assist(self):
        for text in (None, '  ', 'x' * 501):
            result = await self.converse(text)
            self.assertEqual(result.kwargs, {'message': 'invalid_value'})
        self.call.assert_not_awaited()

