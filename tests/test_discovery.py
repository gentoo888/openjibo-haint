"""Zeroconf discovery offers only hosts whose BEacon answers, with the robot name and IP filled in."""
import ipaddress
import sys
import types
import unittest
from unittest.mock import patch

import test_light_commands  # noqa: F401  (installs the homeassistant/aiohttp doubles)
from test_light_commands import module

vol = sys.modules.get('voluptuous') or module('voluptuous')
for name, value in dict(Schema=lambda s: s, Required=lambda k, default=None: (k, default),
                        Optional=lambda k, default=None: (k, default), UNDEFINED=object()).items():
    setattr(vol, name, getattr(vol, name, value))


class ConfigFlow:
    def __init_subclass__(cls, **kw):
        pass

    def __init__(self):
        self.context, self._unique_id = {}, None

    async def async_set_unique_id(self, uid):
        self._unique_id = uid

    def _abort_if_unique_id_configured(self):
        pass

    def _async_abort_entries_match(self, match):
        pass

    def async_abort(self, reason):
        return {'type': 'abort', 'reason': reason}

    def async_show_menu(self, step_id, menu_options):
        return {'type': 'menu', 'step_id': step_id}

    def async_show_form(self, step_id, data_schema, errors):
        return {'type': 'form', 'step_id': step_id, 'schema': data_schema}


sys.modules['homeassistant.config_entries'].ConfigFlow = ConfigFlow
module('homeassistant.helpers.service_info').__path__ = []
module('homeassistant.helpers.service_info.zeroconf').ZeroconfServiceInfo = object
from custom_components.jibo import config_flow  # noqa: E402


def info(ip):
    return types.SimpleNamespace(ip_address=ipaddress.ip_address(ip))


class DiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_jibo_is_offered_with_ip_prefilled(self):
        flow = config_flow.JiboConfigFlow()
        with patch.object(config_flow, '_beacon_hostname', return_value='Auburn-Motor-Salad-Angora'):
            result = await flow.async_step_zeroconf(info('192.168.7.165'))
        self.assertEqual(result, {'type': 'menu', 'step_id': 'user'})
        self.assertEqual(flow.context['title_placeholders'], {'name': 'Auburn-Motor-Salad-Angora'})
        form = await flow.async_step_five_x1()
        self.assertIn((config_flow.CONF_JIBO_IP, '192.168.7.165'), form['schema'])

    async def test_other_ssh_hosts_are_ignored(self):
        flow = config_flow.JiboConfigFlow()
        with patch.object(config_flow, '_beacon_hostname', return_value=None):
            self.assertEqual((await flow.async_step_zeroconf(info('192.168.7.101')))['reason'], 'not_jibo')
        self.assertEqual((await flow.async_step_zeroconf(info('fd2a::1')))['reason'], 'not_jibo')
