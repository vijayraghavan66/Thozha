"""Guard the model-facing development tool contract against redundant approval guidance."""

from openjarvis.tasks.jarvis_dev_mcp import TOOLS


def test_plan_capability_tool_descriptions_preserve_independent_boundaries():
    tools = {tool["name"]: tool for tool in TOOLS}
    approve = tools["approve_plan"]["description"].lower()
    write = tools["write_project_file"]["description"].lower()
    tests = tools["run_project_tests"]["description"].lower()
    export = tools["export_project_artifacts"]["description"].lower()

    assert "approval" in approve and "no files are changed" in approve and "until approval" in approve
    assert "valid plan capability" in write
    assert "without another jarvis approval" in write
    assert "legacy exact per-file approval fallback" in write
    assert "valid plan capability" in tests
    assert "without another jarvis approval" in tests
    assert "legacy exact per-test approval fallback" in tests
    assert "shell_exec" not in tools
    plan_schema = tools["approve_plan"]["inputSchema"]["properties"]["plan"]["properties"]
    assert plan_schema["allowed_ops"]["items"]["enum"] == ["write_file", "run_tests", "artifact_manifest", "artifact_export"]
    assert plan_schema["allowed_paths"]["maxItems"] == 24
    assert plan_schema["max_file_bytes"]["maximum"] == 256 * 1024
    assert "artifact_export" in approve
    assert "explicitly listed files" in export
    assert "only the final artifact metadata" in export
