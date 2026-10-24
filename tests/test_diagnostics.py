import pytest

from ha_mcp.client import HAError
from ha_mcp.safety import GuardError
from tests.conftest import _state


@pytest.fixture(autouse=True)
def automations(ha):
    for st in (
        _state("automation.night_path", "on", friendly_name="Night Path", id="1700000000001"),
        _state("automation.yaml_only", "on", friendly_name="YAML Only"),
    ):
        ha.states[st["entity_id"]] = st


async def test_check_config_valid(make_ops):
    out = await make_ops().check_config()
    assert out == {"result": "valid", "errors": None, "warnings": None}


async def test_check_config_invalid_passes_errors_through(make_ops, ha):
    ha.config_check = {"result": "invalid", "errors": "Invalid config for 'light'", "warnings": None}
    out = await make_ops().check_config()
    assert out["result"] == "invalid" and "light" in out["errors"]


async def test_automation_config(make_ops, ha):
    out = await make_ops().get_automation_config("automation.night_path")
    assert out["config"]["alias"] == "Night Path"
    assert "automation/config" in ha.ws_commands


async def test_traces_newest_first(make_ops):
    out = await make_ops().list_automation_traces("automation.night_path")
    assert out["item_id"] == "1700000000001"
    assert [r["run_id"] for r in out["runs"]] == ["r2", "r1"]
    assert out["runs"][0]["result"] == "failed_conditions"
    assert out["total"] == 2


async def test_traces_limit(make_ops):
    out = await make_ops().list_automation_traces("automation.night_path", limit=1)
    assert len(out["runs"]) == 1 and out["total"] == 2


async def test_trace_defaults_to_latest_and_orders_steps(make_ops):
    out = await make_ops().get_automation_trace("automation.night_path")
    assert out["run_id"] == "r2"
    assert [s["path"] for s in out["steps"]] == ["trigger/0", "condition/0"]
    assert out["steps"][1]["result"] is False
    assert out["steps_truncated"] is False


async def test_trace_max_steps(make_ops):
    out = await make_ops().get_automation_trace("automation.night_path", run_id="r1", max_steps=1)
    assert out["run_id"] == "r1" and len(out["steps"]) == 1 and out["steps_truncated"]


async def test_trace_rejects_non_automation(make_ops):
    with pytest.raises(HAError, match="not an automation"):
        await make_ops().list_automation_traces("light.kitchen")


async def test_trace_explains_missing_id(make_ops):
    with pytest.raises(HAError, match="no 'id' attribute"):
        await make_ops().get_automation_trace("automation.yaml_only")


async def test_trace_no_runs_yet(make_ops, ha):
    ha.traces = []
    with pytest.raises(HAError, match="No stored traces"):
        await make_ops().get_automation_trace("automation.night_path")


async def test_backup_prefers_core_backup(make_ops, ha):
    out = await make_ops().create_backup()
    assert out["called"] == "backup.create_automatic"
    assert ha.backups_made == ["backup.create_automatic"]


async def test_backup_falls_back_to_supervisor(make_ops, ha):
    ha.backup_services = {("hassio", "backup_full")}
    out = await make_ops().create_backup()
    assert out["called"] == "hassio.backup_full"


async def test_backup_none_available(make_ops, ha):
    ha.backup_services = set()
    with pytest.raises(HAError, match="No backup service"):
        await make_ops().create_backup()


async def test_backup_respects_allowed_domains(make_ops, ha):
    with pytest.raises(GuardError):
        await make_ops(allowed_domains=frozenset({"light"})).create_backup()
    assert ha.backups_made == []
