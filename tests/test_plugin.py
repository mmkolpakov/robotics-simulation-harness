from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tests.support import write_extended_scenario

FIXTURES = Path(__file__).parent / "fixtures" / "simulation"


def run_isolated(
    pytester: pytest.Pytester,
    *args: str,
    load_plugin: bool = True,
) -> pytest.RunResult:
    config = pytester.makeini("[pytest]\n")
    plugin_args = ("-p", "robotics_acceptance_harness.plugin") if load_plugin else ()
    return pytester.runpytest(
        "-c",
        str(config),
        "--rootdir",
        str(pytester.path),
        *plugin_args,
        *args,
    )


def make_test(pytester: pytest.Pytester) -> Path:
    return pytester.makepyfile(
        """
        def test_scenario_is_available(robotics_scenario):
            assert robotics_scenario["scenario_id"]
        """
    )


def run_with_simulation(pytester: pytest.Pytester) -> pytest.RunResult:
    test_file = make_test(pytester)
    return run_isolated(
        pytester,
        test_file.name,
        "--robotics-scenario",
        str(FIXTURES / "scenario.yaml"),
        "--robotics-runtime",
        str(FIXTURES / "runtime.yaml"),
    )


def test_simulation_scenario_runs_tests(pytester: pytest.Pytester) -> None:
    result = run_with_simulation(pytester)
    result.assert_outcomes(passed=1)


def test_installed_package_does_not_activate_pytest_plugin(pytester: pytest.Pytester) -> None:
    test_file = pytester.makepyfile("def test_plain_project():\n    assert True\n")

    result = run_isolated(pytester, test_file.name, load_plugin=False)

    result.assert_outcomes(passed=1)


def test_bundle_exposes_runtime_manifest(pytester: pytest.Pytester) -> None:
    test_file = pytester.makepyfile(
        """
        def test_bundle(robotics_bundle):
            assert robotics_bundle.runtime.schema_version == "runtime-manifest.v1"
            assert robotics_bundle.runtime.data["workload"]["kind"] == "none"
        """
    )
    result = run_isolated(
        pytester,
        test_file.name,
        "--robotics-scenario",
        str(FIXTURES / "scenario.yaml"),
        "--robotics-runtime",
        str(FIXTURES / "runtime.yaml"),
    )
    result.assert_outcomes(passed=1)


def test_plugin_loads_extension_schema_by_canonical_uri(pytester: pytest.Pytester) -> None:
    test_file = make_test(pytester)
    scenario, schema, uri = write_extended_scenario(pytester.path, FIXTURES / "scenario.yaml")
    result = run_isolated(
        pytester,
        test_file.name,
        "--robotics-scenario",
        str(scenario),
        "--robotics-runtime",
        str(FIXTURES / "runtime.yaml"),
        "--robotics-extension-schema",
        f"{uri}={schema}",
    )

    result.assert_outcomes(passed=1)


def test_bundle_requires_runtime_manifest(pytester: pytest.Pytester) -> None:
    test_file = make_test(pytester)
    result = run_isolated(
        pytester,
        test_file.name,
        "--robotics-scenario",
        str(FIXTURES / "scenario.yaml"),
    )

    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*requires a runtime manifest*"])


def test_missing_scenario_option_is_a_usage_error(pytester: pytest.Pytester) -> None:
    test_file = make_test(pytester)
    result = run_isolated(pytester, test_file.name)

    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*--robotics-scenario PATH is required*"])


def test_malformed_yaml_is_a_usage_error(pytester: pytest.Pytester) -> None:
    test_file = make_test(pytester)
    scenario = pytester.makefile(".yaml", scenario="expected_ros_graph: [")
    result = run_isolated(
        pytester,
        test_file.name,
        "--robotics-scenario",
        str(scenario),
        "--robotics-runtime",
        str(FIXTURES / "runtime.yaml"),
    )

    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*cannot parse robotics scenario*"])


def test_invalid_contract_reports_json_path(pytester: pytest.Pytester) -> None:
    test_file = make_test(pytester)
    document = yaml.safe_load((FIXTURES / "scenario.yaml").read_text(encoding="utf-8"))
    document["execution"]["target_environment"] = "staging"
    scenario = pytester.makefile(".yaml", scenario=yaml.safe_dump(document))
    result = run_isolated(
        pytester,
        test_file.name,
        "--robotics-scenario",
        str(scenario),
        "--robotics-runtime",
        str(FIXTURES / "runtime.yaml"),
    )

    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*$.execution.target_environment:*"])


def test_scenario_fixture_is_deeply_immutable(pytester: pytest.Pytester) -> None:
    test_file = pytester.makepyfile(
        """
        import pytest

        def test_scenario_is_immutable(robotics_scenario):
            with pytest.raises(TypeError):
                robotics_scenario["seed"] = 2
            with pytest.raises(TypeError):
                robotics_scenario["timeouts"]["startup_sec"] = 2
        """
    )
    result = run_isolated(
        pytester,
        test_file.name,
        "--robotics-scenario",
        str(FIXTURES / "scenario.yaml"),
        "--robotics-runtime",
        str(FIXTURES / "runtime.yaml"),
    )
    result.assert_outcomes(passed=1)


def test_help_has_no_physical_execution_bypass(pytester: pytest.Pytester) -> None:
    result = run_isolated(pytester, "--help")

    assert result.ret == pytest.ExitCode.OK
    result.stdout.fnmatch_lines(["*--robotics-scenario=PATH*"])
    assert "allow-physical" not in result.stdout.str()
    assert "--robotics-permit" not in result.stdout.str()
