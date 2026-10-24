async def test_error_log_falls_back_to_system_log(make_ops, ha):
    ha.error_log_missing = True
    out = await make_ops().get_error_log(lines=10)
    lines = out.splitlines()
    assert "system_log/list" in ha.ws_commands
    assert "Camera stream lost" in lines[0] and "Device offline [x3]" in lines[1]


async def test_error_log_rest_still_preferred(make_ops, ha):
    out = await make_ops().get_error_log(lines=1)
    assert out.startswith("line") and "system_log/list" not in ha.ws_commands
