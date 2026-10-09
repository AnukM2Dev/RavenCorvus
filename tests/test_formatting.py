from ha_mcp.formatting import (
    compact_history,
    compact_logbook,
    compact_service_domains,
    compact_state,
    compact_states,
    render_table,
    summarize_changes,
    tail_text,
)

STATE = {
    "entity_id": "sensor.outdoor_temp",
    "state": "21.5",
    "attributes": {
        "friendly_name": "Outdoor Temperature",
        "unit_of_measurement": "°C",
        "device_class": "temperature",
        "noisy": "x" * 5000,
        "supported_features": 44,
    },
    "last_changed": "t1",
    "last_updated": "t2",
}


def test_compact_state_drops_noise_and_keeps_useful_attrs():
    out = compact_state(STATE)
    assert out == {
        "entity_id": "sensor.outdoor_temp",
        "state": "21.5",
        "name": "Outdoor Temperature",
        "attrs": {"unit_of_measurement": "°C", "device_class": "temperature"},
    }
    assert "noisy" not in str(out)


def test_compact_state_full_returns_everything():
    out = compact_state(STATE, full=True)
    assert out["attributes"]["noisy"] == "x" * 5000
    assert out["last_changed"] == "t1"


def test_compact_state_clips_long_values_and_lists():
    st = {
        "entity_id": "climate.hall",
        "state": "heat",
        "attributes": {"hvac_modes": [f"m{i}" for i in range(20)], "temperature": "y" * 500},
    }
    out = compact_state(st)["attrs"]
    assert len(out["hvac_modes"]) == 9 and out["hvac_modes"][-1] == "… +12 more"
    assert len(out["temperature"]) == 120 and out["temperature"].endswith("…")


def test_compact_states_adds_area():
    out = compact_states([STATE], area_map={"sensor.outdoor_temp": {"area": "Porch", "area_id": "porch"}})
    assert out[0]["area"] == "Porch"


def test_summarize_changes_reports_from_and_to():
    before = {"light.a": {"entity_id": "light.a", "state": "off"}}
    changed = [
        {"entity_id": "light.a", "state": "on", "attributes": {"brightness": 255, "other": 1}},
        {"entity_id": "light.b", "state": "on", "attributes": {}},
    ]
    out = summarize_changes(before, changed)
    assert out[0] == {"entity_id": "light.a", "from": "off", "to": "on", "attrs": {"brightness": 255}}
    assert out[1]["from"] is None  # not read beforehand


def test_compact_service_domains_flattens_and_filters():
    raw = [
        {"domain": "light", "services": {"turn_on": {"description": " On ", "fields": {"b": {}, "a": {}}}}},
        {"domain": "lock", "services": {"lock": {}}},
    ]
    assert compact_service_domains(raw) == [
        {"service": "light.turn_on", "description": "On", "fields": ["a", "b"]},
        {"service": "lock.lock", "description": "", "fields": []},
    ]
    assert [r["service"] for r in compact_service_domains(raw, domain="lock")] == ["lock.lock"]


def test_compact_history_takes_entity_id_from_first_point():
    series = [
        [
            {"entity_id": "light.a", "state": "off", "last_changed": "t1"},
            {"state": "on", "last_changed": "t2"},
        ],
        [],
    ]
    assert compact_history(series) == [
        {"entity_id": "light.a", "count": 2, "changes": [["t1", "off"], ["t2", "on"]]}
    ]


def test_compact_logbook_keeps_most_recent_n():
    entries = [{"when": str(i), "name": "n", "message": "m", "entity_id": "e"} for i in range(10)]
    out = compact_logbook(entries, limit=3)
    assert [e["when"] for e in out] == ["7", "8", "9"]


def test_tail_text_limits_lines_and_chars():
    text = "\n".join(f"line {i}" for i in range(500))
    assert tail_text(text, lines=3) == "line 497\nline 498\nline 499"
    assert len(tail_text("x" * 100, lines=1, max_chars=10)) == 11  # ellipsis + 10


def test_render_table_aligns_columns():
    rows = [{"a": "x", "b": "long value"}, {"a": "yy", "b": "z"}]
    out = render_table(rows, ["a", "b"]).splitlines()
    assert out[0].startswith("A ") and "B" in out[0]
    assert len(out) == 4
    assert render_table([], ["a"]) == "(none)"
