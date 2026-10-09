import pytest

from ha_mcp.config import DEFAULT_SENSITIVE_DOMAINS
from ha_mcp.safety import GuardError, WriteGuard, extract_entity_ids

from .conftest import make_settings


def guard(**kw) -> WriteGuard:
    return WriteGuard(make_settings(**kw))


def test_ordinary_domain_allowed_by_default():
    d = guard().check_service("light", "turn_on", {"entity_id": "light.kitchen"})
    assert d.allowed
    assert d.entities == ("light.kitchen",)


def test_read_only_blocks_everything_and_names_setting():
    d = guard(read_only=True).check_service("light", "turn_on", {"entity_id": "light.kitchen"})
    assert not d.allowed
    assert "HASS_READ_ONLY" in d.reason
    with pytest.raises(GuardError):
        d.raise_if_blocked()


@pytest.mark.parametrize("domain", sorted(DEFAULT_SENSITIVE_DOMAINS))
def test_sensitive_domains_blocked_by_service_domain(domain):
    d = guard().check_service(domain, "anything", {})
    assert not d.allowed
    assert "HASS_ALLOW_SENSITIVE_DOMAINS" in d.reason
    assert domain in d.reason


def test_sensitive_domain_blocked_via_entity_even_on_other_service_domain():
    # script.* that names a lock entity must not slip past the domain check
    d = guard().check_service("script", "run", {"entity_id": ["script.x", "lock.front_door"]})
    assert not d.allowed
    assert "lock" in d.reason


def test_sensitive_opt_in_is_per_domain():
    g = guard(allow_sensitive=frozenset({"climate"}))
    assert g.check_service("climate", "set_temperature", {"entity_id": "climate.hall"}).allowed
    assert not g.check_service("lock", "unlock", {"entity_id": "lock.front_door"}).allowed


def test_denylist_exact_and_glob_win_over_opt_in():
    g = guard(
        allow_sensitive=DEFAULT_SENSITIVE_DOMAINS,
        denied_entities=frozenset({"lock.front_door", "switch.server_*"}),
    )
    d = g.check_service("lock", "unlock", {"entity_id": "lock.front_door"})
    assert not d.allowed and "HASS_DENIED_ENTITIES" in d.reason
    d = g.check_service("switch", "turn_off", {"entity_id": "switch.server_rack"})
    assert not d.allowed
    assert g.check_service("switch", "turn_off", {"entity_id": "switch.fan"}).allowed


def test_denylist_also_matches_the_service_name_so_direct_script_calls_are_stopped():
    # script.my_script is called as a *service*; there is no entity_id in the payload
    g = guard(denied_entities=frozenset({"script.*"}))
    d = g.check_service("script", "my_script", {})
    assert not d.allowed
    assert "script.my_script" in d.reason and "HASS_DENIED_ENTITIES" in d.reason
    # ...and via the entity form too
    assert not g.check_service("script", "turn_on", {"entity_id": "script.my_script"}).allowed
    # unrelated services are untouched
    assert g.check_service("light", "turn_on", {"entity_id": "light.kitchen"}).allowed


def test_denylist_is_case_insensitive():
    g = guard(denied_entities=frozenset({"light.kitchen"}))
    assert not g.check_service("light", "turn_on", {"entity_id": "LIGHT.Kitchen"}).allowed


def test_allowed_domains_narrows_surface():
    g = guard(allowed_domains=frozenset({"light"}))
    assert g.check_service("light", "turn_on", {"entity_id": "light.kitchen"}).allowed
    d = g.check_service("switch", "turn_on", {"entity_id": "switch.fan"})
    assert not d.allowed and "HASS_ALLOWED_DOMAINS" in d.reason


def test_allowed_domains_does_not_unlock_sensitive():
    g = guard(allowed_domains=frozenset({"lock"}))
    assert not g.check_service("lock", "unlock", {"entity_id": "lock.front_door"}).allowed


def test_fan_out_service_with_area_target_is_blocked_by_default():
    d = guard().check_service("homeassistant", "turn_off", {"area_id": "kitchen"})
    assert not d.allowed and "area/device/label" in d.reason


def test_fan_out_service_with_named_entities_is_judged_by_those_entities():
    g = guard()
    assert g.check_service("homeassistant", "turn_off", {"entity_id": "light.kitchen"}).allowed
    assert not g.check_service("homeassistant", "turn_off", {"entity_id": "lock.front_door"}).allowed


def test_fan_out_with_entity_and_area_is_still_blind():
    d = guard().check_service(
        "homeassistant", "turn_off", {"entity_id": "light.kitchen", "area_id": "kitchen"}
    )
    assert not d.allowed


def test_fan_out_allowed_once_all_sensitive_domains_opted_in():
    g = guard(allow_sensitive=DEFAULT_SENSITIVE_DOMAINS)
    assert g.check_service("homeassistant", "turn_off", {"area_id": "kitchen"}).allowed


def test_domain_specific_service_with_area_target_is_not_blind():
    # light.turn_off can only ever reach lights
    assert guard().check_service("light", "turn_off", {"area_id": "kitchen"}).allowed


def test_set_state_applies_same_checks():
    assert guard().check_set_state("input_boolean.guest").allowed
    assert not guard().check_set_state("lock.front_door").allowed
    assert not guard(read_only=True).check_set_state("input_boolean.guest").allowed


def test_fire_event_needs_full_opt_in():
    assert not guard().check_fire_event("my_event").allowed
    assert not guard(read_only=True, allow_sensitive=DEFAULT_SENSITIVE_DOMAINS).check_fire_event("e").allowed
    assert guard(allow_sensitive=DEFAULT_SENSITIVE_DOMAINS).check_fire_event("e").allowed


def test_extract_entity_ids_shapes():
    assert extract_entity_ids(None) == []
    assert extract_entity_ids({"entity_id": "light.a, light.b"}) == ["light.a", "light.b"]
    assert extract_entity_ids({"entity_id": ["light.a", "light.b"]}) == ["light.a", "light.b"]
    assert extract_entity_ids({"target": {"entity_id": "light.a"}}) == ["light.a"]
