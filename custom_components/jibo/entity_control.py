"""Allowlisted everyday entity actions, resolved and validated inside HA."""
import math
import re

ACTIONS = {
    'light': {'turn_on', 'turn_off', 'brightness', 'color', 'color_temperature'},
    'climate': {'temperature', 'hvac_mode'},
    'switch': {'turn_on', 'turn_off'},
    'fan': {'turn_on', 'turn_off', 'percentage'},
    'cover': {'open', 'close', 'stop', 'position'},
    'scene': {'activate'}, 'script': {'activate'},
}


def normalize(value):
    return re.sub(r'[\W_]+', ' ', str(value).lower()).strip()


def names(entry, state):
    return {normalize(n) for n in [entry.entity_id, entry.entity_id.split('.', 1)[1],
            getattr(entry, 'name', None), getattr(entry, 'original_name', None),
            getattr(state, 'name', None), *(getattr(entry, 'aliases', None) or [])] if n}


def number(value):
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValueError('invalid_value') from None
    if not math.isfinite(result):
        raise ValueError('invalid_value')
    return result


def bounded(value, low, high):
    result = number(value)
    if not low <= result <= high:
        raise ValueError('invalid_value')
    return result


def service_data(domain, action, payload, state, temperature_unit=None):
    """Preflight each entity before issuing any service calls for a group."""
    if state is None or state.state == 'unavailable' or (state.state == 'unknown' and domain != 'scene'):
        raise ValueError('unavailable')
    attrs = state.attributes
    value = payload.get('value')
    unit = str(payload.get('unit', '')).lower()
    if action in ('brightness', 'percentage', 'position') and unit not in ('', 'percent', '%'):
        raise ValueError('invalid_value')
    if action == 'temperature' and unit not in ('', 'degrees', 'celsius', 'fahrenheit', 'degrees celsius', 'degrees fahrenheit'):
        raise ValueError('invalid_value')
    features = int(attrs.get('supported_features', 0))
    if action in ('turn_on', 'turn_off'):
        return action, {}
    if domain == 'light':
        modes = set(attrs.get('supported_color_modes', []))
        if action == 'brightness':
            if not modes - {'onoff', 'unknown'}:
                raise ValueError('unsupported')
            return 'turn_on', {'brightness_pct': bounded(value, 0, 100)}
        if action == 'color':
            if not modes & {'hs', 'xy', 'rgb', 'rgbw', 'rgbww'}:
                raise ValueError('unsupported')
            # HA validates named CSS colors as part of the service schema.
            if value not in {'red', 'green', 'blue', 'yellow', 'orange', 'purple', 'pink',
                             'white', 'cyan', 'magenta'}:
                raise ValueError('invalid_value')
            return 'turn_on', {'color_name': value}
        if action == 'color_temperature':
            if 'color_temp' not in modes and 'rgbww' not in modes:
                raise ValueError('unsupported')
            low = attrs.get('min_color_temp_kelvin', 2000)
            high = attrs.get('max_color_temp_kelvin', 6500)
            kelvin = min(high, max(low, 2700 if value == 'warm white' else 6500)) if value in ('warm white', 'cool white') else bounded(value, low, high)
            return 'turn_on', {'color_temp_kelvin': round(kelvin)}
    if domain == 'fan' and action == 'percentage':
        if not features & 1:
            raise ValueError('unsupported')
        return 'set_percentage', {'percentage': bounded(value, 0, 100)}
    if domain == 'cover':
        bit, service = {'open': (1, 'open_cover'), 'close': (2, 'close_cover'),
                        'stop': (8, 'stop_cover'), 'position': (4, 'set_cover_position')}[action]
        if not features & bit:
            raise ValueError('unsupported')
        return service, {'position': bounded(value, 0, 100)} if action == 'position' else {}
    if domain == 'climate':
        if action == 'hvac_mode':
            mode = str(value).replace(' ', '_')
            if mode not in attrs.get('hvac_modes', []):
                raise ValueError('unsupported')
            return 'set_hvac_mode', {'hvac_mode': mode}
        if not features & 1:
            raise ValueError('unsupported')
        temp = number(value)
        unit = str(payload.get('unit', '')).lower()
        source = 'C' if 'celsius' in unit else 'F' if 'fahrenheit' in unit else None
        target = str(temperature_unit or attrs.get('temperature_unit', ''))
        if source == 'C' and target in ('°F', 'F'):
            temp = temp * 9 / 5 + 32
        elif source == 'F' and target in ('°C', 'C'):
            temp = (temp - 32) * 5 / 9
        temp = bounded(temp, attrs.get('min_temp', -273.15), attrs.get('max_temp', 1000))
        return 'set_temperature', {'temperature': temp}
    return 'turn_on', {}  # Only allowlisted scene/script activation reaches here.


async def handle_entity_control(coordinator, payload, request_id, *, allow_legacy_fallback=False):
    from homeassistant.helpers import entity_registry as er, device_registry as dr
    from homeassistant.helpers import area_registry as ar

    domain, action = payload.get('domain'), payload.get('action')
    scope = payload.get('scope', 'named')
    if domain not in ACTIONS or action not in ACTIONS[domain] or scope not in {'room', 'area', 'all', 'named', 'entity'}:
        raise ValueError('unsupported')
    if domain in ('scene', 'script') and scope not in ('named', 'entity'):
        raise ValueError('unsupported')
    registry = er.async_get(coordinator.hass)
    devices = dr.async_get(coordinator.hass)
    entries = [e for e in registry.entities.values() if e.domain == domain and not getattr(e, 'disabled_by', None)]
    target = normalize(payload.get('targetName', ''))
    area_id = None
    if scope == 'room':
        area_id = coordinator._get_jibo_area_id()
        if not area_id:
            await coordinator._send_command_result(request_id, 'not_found', message='missing_area')
            return True
    elif scope == 'area':
        areas = ar.async_get(coordinator.hass)
        matches = [a for a in areas.areas.values() if target in
                   {normalize(a.name), normalize(a.id), *(normalize(n) for n in (getattr(a, 'aliases', None) or []))}]
        if len(matches) != 1:
            await coordinator._send_command_result(request_id, 'not_found', message='missing_area')
            return True
        area_id = matches[0].id
    if area_id is not None:
        entries = [e for e in entries if coordinator._resolve_entity_area_id(registry, devices, e) == area_id]
    elif scope == 'entity':
        entries = [e for e in entries if e.entity_id == payload.get('entityId')]
    elif scope == 'named':
        # Exact entity/alias names take priority; typed nouns may be omitted in speech.
        exact = [e for e in entries if target in names(e, coordinator.hass.states.get(e.entity_id))]
        if exact:
            entries = exact
        else:
            def short(value):
                return normalize(re.sub(r'\b(lights?|lamps?|thermostats?|switch(?:es)?|fans?|covers?|blinds?|shades?|curtains?)\b', '', value))
            entries = [e for e in entries if target and short(target) in
                       {short(n) for n in names(e, coordinator.hass.states.get(e.entity_id))}]
        if not entries and target and domain not in ('scene', 'script'):
            areas = ar.async_get(coordinator.hass)
            matches = [a for a in areas.areas.values() if target in
                       {normalize(a.name), normalize(a.id), *(normalize(n) for n in (getattr(a, 'aliases', None) or []))}]
            if len(matches) == 1:
                entries = [e for e in registry.entities.values() if e.domain == domain and
                           not getattr(e, 'disabled_by', None) and
                           coordinator._resolve_entity_area_id(registry, devices, e) == matches[0].id]
        if len(entries) > 1:
            await coordinator._send_command_result(request_id, 'needs_clarification', candidates=[
                {'entityId': e.entity_id, 'name': coordinator._friendly_name(e.entity_id) + ' (' + e.entity_id + ')'} for e in entries])
            return True
    if domain == 'climate' and scope == 'room' and len(entries) > 1:
        await coordinator._send_command_result(request_id, 'needs_clarification', candidates=[
            {'entityId': e.entity_id, 'name': coordinator._friendly_name(e.entity_id) + ' (' + e.entity_id + ')'} for e in entries])
        return True
    if not entries:
        if allow_legacy_fallback:
            return False
        await coordinator._send_command_result(request_id, 'not_found', heard_name=payload.get('targetName'))
        return True
    # Construct every call first: an unsupported member cannot cause a partial group change.
    units = getattr(getattr(getattr(coordinator.hass, 'config', None), 'units', None), 'temperature_unit', None)
    calls = [(e.entity_id, *service_data(domain, action, payload, coordinator.hass.states.get(e.entity_id), units)) for e in entries]
    if hasattr(coordinator.hass.services, 'has_service') and any(
            not coordinator.hass.services.has_service(domain, service) for _, service, _ in calls):
        raise ValueError('unsupported')
    for entity_id, service, data in calls:
        await coordinator.hass.services.async_call(domain, service, {'entity_id': entity_id, **data}, blocking=True)
    await coordinator._send_command_result(request_id, 'ok',
        matched_name=coordinator._friendly_name(entries[0].entity_id) if len(entries) == 1 else None,
        heard_name=payload.get('targetName'))
    return True
