import json

import pytest

from ha_mcp.cli import _main


@pytest.fixture
def run_cli(make_ops, capsys):
    async def _run(*argv: str, **settings) -> tuple[int, str, str]:
        ops = make_ops(**settings)
        code = await _main(list(argv), lambda: ops)
        out = capsys.readouterr()
        return code, out.out, out.err

    return _run


async def test_status_json(run_cli):
    code, out, _ = await run_cli("status")
    assert code == 0
    assert json.loads(out)["version"] == "2026.10.0"


async def test_entities_table_with_units_and_area(run_cli):
    code, out, _ = await run_cli("entities", "--domain", "sensor")
    assert code == 0
    assert "ENTITY_ID" in out and "sensor.outdoor_temp" in out and "21.5 °C" in out
    code, out, _ = await run_cli("entities", "--area", "kitchen")
    assert "light.kitchen" in out and "Kitchen" in out


async def test_entities_json_format(run_cli):
    code, out, _ = await run_cli("-f", "json", "entities", "--domain", "light")
    assert code == 0
    assert json.loads(out)["total"] == 2


async def test_truncation_hint_in_table(run_cli):
    _, out, _ = await run_cli("entities", "--limit", "2")
    assert "showing 2 of 7" in out


async def test_state_and_search(run_cli):
    _, out, _ = await run_cli("state", "light.kitchen", "--full")
    assert json.loads(out)["attributes"]["brightness"] == 200
    _, out, _ = await run_cli("search", "attic fan")
    assert "switch.fan" in out


async def test_call_with_entity_shorthand_and_data(run_cli, ha):
    code, out, _ = await run_cli(
        "call", "light", "turn_off", "-e", "light.kitchen", "-d", '{"transition": 2}'
    )
    assert code == 0
    assert json.loads(out)["changed"][0]["to"] == "off"
    assert ha.states["light.kitchen"]["state"] == "off"


async def test_call_blocked_exits_2_with_message_on_stderr(run_cli, ha):
    code, out, err = await run_cli("call", "lock", "unlock", "-e", "lock.front_door")
    assert code == 2 and out == ""
    assert "blocked" in err and "HASS_ALLOW_SENSITIVE_DOMAINS" in err
    assert ha.states["lock.front_door"]["state"] == "locked"


async def test_call_dry_run(run_cli, ha):
    code, out, _ = await run_cli("call", "light", "turn_off", "-e", "light.kitchen", "--dry-run")
    assert code == 0 and json.loads(out)["dry_run"] is True
    assert ha.states["light.kitchen"]["state"] == "on"


async def test_bad_json_is_usage_error(run_cli):
    code, _, err = await run_cli("call", "light", "turn_on", "-d", "{nope")
    assert code == 2 and "valid JSON" in err
    code, _, err = await run_cli("call", "light", "turn_on", "-d", "[1]")
    assert code == 2 and "JSON object" in err


async def test_ha_error_exits_1(run_cli):
    code, _, err = await run_cli("state", "light.nope")
    assert code == 1 and "404" in err


async def test_read_only_blocks_set_state(run_cli):
    code, _, err = await run_cli("set-state", "input_boolean.x", "on", read_only=True)
    assert code == 2 and "HASS_READ_ONLY" in err


async def test_missing_config_exits_2(monkeypatch, capsys):
    monkeypatch.delenv("HASS_URL", raising=False)
    monkeypatch.delenv("HASS_TOKEN", raising=False)
    assert await _main(["status"], None) == 2
    assert "HASS_URL" in capsys.readouterr().err
