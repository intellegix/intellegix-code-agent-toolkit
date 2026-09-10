"""Tests for config module."""

import json
from pathlib import Path

import pytest

from config import WorkflowConfig, load_config


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    return tmp_path


class TestWorkflowConfig:
    def test_defaults(self) -> None:
        config = WorkflowConfig()
        assert config.limits.max_iterations == 50
        assert config.limits.timeout_seconds == 300
        assert config.perplexity.research_timeout_seconds == 600
        assert config.perplexity.provider == "perplexity"
        assert config.perplexity.headful is True
        assert config.perplexity.perplexity_mode == "research"
        assert config.claude.model == "sonnet"
        assert config.claude.dangerously_skip_permissions is True
        assert len(config.patterns.completion_markers) == 3
        assert "PROJECT_COMPLETE" in config.patterns.completion_markers

    def test_custom_values(self) -> None:
        config = WorkflowConfig(
            limits={"max_iterations": 100, "timeout_seconds": 600},
            claude={"model": "opus"},
        )
        assert config.limits.max_iterations == 100
        assert config.claude.model == "opus"

    def test_validation_rejects_bad_values(self) -> None:
        with pytest.raises(Exception):
            WorkflowConfig(limits={"max_iterations": 0})  # ge=1

        with pytest.raises(Exception):
            WorkflowConfig(limits={"timeout_seconds": 10})  # ge=30


class TestLoadConfig:
    def test_load_valid_config(self, config_dir: Path) -> None:
        config_file = config_dir / "config.json"
        config_file.write_text(
            json.dumps({
                "limits": {"max_iterations": 25},
                "claude": {"model": "opus"},
            }),
            encoding="utf-8",
        )

        result = load_config(config_file)
        assert result.success
        assert result.data is not None
        assert result.data.limits.max_iterations == 25
        assert result.data.claude.model == "opus"

    def test_load_missing_file_returns_defaults(self, config_dir: Path) -> None:
        result = load_config(config_dir / "nonexistent.json")
        assert result.success
        assert result.data is not None
        assert result.data.limits.max_iterations == 50

    def test_load_invalid_json(self, config_dir: Path) -> None:
        config_file = config_dir / "config.json"
        config_file.write_text("not json {{{", encoding="utf-8")

        result = load_config(config_file)
        assert not result.success
        assert result.error_code == "JSON_ERROR"

    def test_load_invalid_values(self, config_dir: Path) -> None:
        config_file = config_dir / "config.json"
        config_file.write_text(
            json.dumps({"limits": {"max_iterations": -5}}),
            encoding="utf-8",
        )

        result = load_config(config_file)
        assert not result.success
        assert result.error_code == "VALIDATION_ERROR"

    def test_load_empty_object_uses_defaults(self, config_dir: Path) -> None:
        config_file = config_dir / "config.json"
        config_file.write_text("{}", encoding="utf-8")

        result = load_config(config_file)
        assert result.success
        assert result.data.limits.max_iterations == 50

    def test_load_partial_config_fills_defaults(self, config_dir: Path) -> None:
        config_file = config_dir / "config.json"
        config_file.write_text(
            json.dumps({"perplexity": {"provider": "youcom", "perplexity_mode": "labs"}}),
            encoding="utf-8",
        )

        result = load_config(config_file)
        assert result.success
        assert result.data.perplexity.provider == "youcom"
        assert result.data.perplexity.perplexity_mode == "labs"
        assert result.data.limits.max_iterations == 50  # default


class TestBudgetConfig:
    def test_max_total_budget_default(self) -> None:
        config = WorkflowConfig()
        assert config.limits.max_total_budget_usd == 50.0

    def test_max_total_budget_custom(self) -> None:
        config = WorkflowConfig(limits={"max_total_budget_usd": 100.0})
        assert config.limits.max_total_budget_usd == 100.0

    def test_max_total_budget_rejects_zero(self) -> None:
        with pytest.raises(Exception):
            WorkflowConfig(limits={"max_total_budget_usd": 0})


class TestRetryConfig:
    def test_retry_defaults(self) -> None:
        from config import RetryConfig
        config = RetryConfig()
        assert config.max_retries == 3
        assert config.base_delay_seconds == 1.0
        assert config.circuit_breaker_threshold == 5

    def test_retry_in_workflow_config(self) -> None:
        config = WorkflowConfig()
        assert config.retry.max_retries == 3


class TestStagnationConfig:
    def test_stagnation_defaults(self) -> None:
        from config import StagnationConfig
        cfg = StagnationConfig()
        assert cfg.enabled is True
        assert cfg.window_size == 3
        assert cfg.low_turn_threshold == 2
        assert cfg.max_consecutive_timeouts == 2

    def test_stagnation_in_workflow_config(self) -> None:
        config = WorkflowConfig()
        assert config.stagnation.enabled is True
        assert config.stagnation.window_size == 3

    def test_stagnation_custom_values(self) -> None:
        config = WorkflowConfig(
            stagnation={"window_size": 5, "low_turn_threshold": 3, "max_consecutive_timeouts": 4}
        )
        assert config.stagnation.window_size == 5
        assert config.stagnation.low_turn_threshold == 3
        assert config.stagnation.max_consecutive_timeouts == 4

    def test_stagnation_disabled(self) -> None:
        config = WorkflowConfig(stagnation={"enabled": False})
        assert config.stagnation.enabled is False

    def test_stagnation_validation_rejects_bad_window(self) -> None:
        with pytest.raises(Exception):
            WorkflowConfig(stagnation={"window_size": 1})  # ge=2


class TestModelAwareConfig:
    def test_model_timeout_multipliers_defaults(self) -> None:
        config = WorkflowConfig()
        assert config.limits.model_timeout_multipliers["opus"] == 2.0
        assert config.limits.model_timeout_multipliers["sonnet"] == 1.0
        assert config.limits.model_timeout_multipliers["haiku"] == 0.5

    def test_model_timeout_multipliers_custom(self) -> None:
        config = WorkflowConfig(
            limits={"model_timeout_multipliers": {"opus": 3.0, "sonnet": 1.5}}
        )
        assert config.limits.model_timeout_multipliers["opus"] == 3.0
        assert config.limits.model_timeout_multipliers["sonnet"] == 1.5
        assert "haiku" not in config.limits.model_timeout_multipliers

    def test_model_timeout_multipliers_unknown_model_not_present(self) -> None:
        config = WorkflowConfig()
        assert config.limits.model_timeout_multipliers.get("unknown") is None

    def test_model_max_turns_override_defaults(self) -> None:
        config = WorkflowConfig()
        assert config.limits.model_max_turns_override["opus"] == 25
        assert "sonnet" not in config.limits.model_max_turns_override

    def test_model_max_turns_override_empty(self) -> None:
        config = WorkflowConfig(limits={"model_max_turns_override": {}})
        assert config.limits.model_max_turns_override == {}

    def test_model_max_turns_override_custom(self) -> None:
        config = WorkflowConfig(
            limits={"model_max_turns_override": {"opus": 15, "sonnet": 30}}
        )
        assert config.limits.model_max_turns_override["opus"] == 15
        assert config.limits.model_max_turns_override["sonnet"] == 30

    def test_model_stagnation_timeout_overrides_default(self) -> None:
        config = WorkflowConfig()
        assert config.stagnation.model_timeout_overrides["opus"] == 3

    def test_model_stagnation_timeout_overrides_custom(self) -> None:
        config = WorkflowConfig(
            stagnation={"model_timeout_overrides": {"opus": 5, "haiku": 1}}
        )
        assert config.stagnation.model_timeout_overrides["opus"] == 5
        assert config.stagnation.model_timeout_overrides["haiku"] == 1

    def test_timeout_cooldown_defaults(self) -> None:
        config = WorkflowConfig()
        assert config.limits.timeout_cooldown_base_seconds == 60
        assert config.limits.timeout_cooldown_max_seconds == 300

    def test_timeout_cooldown_custom(self) -> None:
        config = WorkflowConfig(
            limits={"timeout_cooldown_base_seconds": 30, "timeout_cooldown_max_seconds": 120}
        )
        assert config.limits.timeout_cooldown_base_seconds == 30
        assert config.limits.timeout_cooldown_max_seconds == 120

    def test_timeout_cooldown_disabled(self) -> None:
        config = WorkflowConfig(limits={"timeout_cooldown_base_seconds": 0})
        assert config.limits.timeout_cooldown_base_seconds == 0

    def test_model_fallback_defaults(self) -> None:
        config = WorkflowConfig()
        assert config.limits.model_fallback == {"opus": "sonnet"}
        assert config.limits.model_fallback_after_timeouts == 2

    def test_model_fallback_custom(self) -> None:
        config = WorkflowConfig(
            limits={"model_fallback": {"opus": "haiku"}, "model_fallback_after_timeouts": 3}
        )
        assert config.limits.model_fallback["opus"] == "haiku"
        assert config.limits.model_fallback_after_timeouts == 3

    def test_model_fallback_empty_disables(self) -> None:
        config = WorkflowConfig(limits={"model_fallback": {}})
        assert config.limits.model_fallback == {}


class TestTraceRotationConfig:
    def test_trace_max_size_default(self) -> None:
        config = WorkflowConfig()
        assert config.limits.trace_max_size_bytes == 10_000_000

    def test_trace_max_size_custom(self) -> None:
        config = WorkflowConfig(limits={"trace_max_size_bytes": 5_000_000})
        assert config.limits.trace_max_size_bytes == 5_000_000

    def test_trace_max_size_zero_unlimited(self) -> None:
        config = WorkflowConfig(limits={"trace_max_size_bytes": 0})
        assert config.limits.trace_max_size_bytes == 0

    def test_trace_max_size_rejects_negative(self) -> None:
        with pytest.raises(Exception):
            WorkflowConfig(limits={"trace_max_size_bytes": -1})


class TestExplorationConfig:
    def test_exploration_defaults(self) -> None:
        from config import ExplorationConfig
        cfg = ExplorationConfig()
        assert cfg.enabled is True
        assert cfg.max_files_to_read == 10
        assert cfg.max_chars_per_file == 3000

    def test_exploration_in_workflow_config(self) -> None:
        config = WorkflowConfig()
        assert config.exploration.enabled is True
        assert config.exploration.max_files_to_read == 10

    def test_exploration_custom_values(self) -> None:
        config = WorkflowConfig(
            exploration={"max_files_to_read": 20, "max_chars_per_file": 5000}
        )
        assert config.exploration.max_files_to_read == 20
        assert config.exploration.max_chars_per_file == 5000

    def test_exploration_validation_rejects_bad_values(self) -> None:
        with pytest.raises(Exception):
            WorkflowConfig(exploration={"max_files_to_read": 0})  # ge=1
        with pytest.raises(Exception):
            WorkflowConfig(exploration={"max_chars_per_file": 100})  # ge=500


class TestVerificationConfig:
    def test_verification_defaults(self) -> None:
        from config import VerificationConfig
        cfg = VerificationConfig()
        assert cfg.enabled is True
        assert cfg.verification_timeout_seconds == 600

    def test_verification_in_workflow_config(self) -> None:
        config = WorkflowConfig()
        assert config.verification.enabled is True
        assert config.verification.verification_timeout_seconds == 600

    def test_verification_custom_values(self) -> None:
        config = WorkflowConfig(
            verification={"enabled": False, "verification_timeout_seconds": 300}
        )
        assert config.verification.enabled is False
        assert config.verification.verification_timeout_seconds == 300

    def test_verification_validation_rejects_bad_values(self) -> None:
        with pytest.raises(Exception):
            WorkflowConfig(verification={"verification_timeout_seconds": 30})  # ge=60


class TestValidationConfig:
    def test_validation_defaults(self) -> None:
        from config import ValidationConfig
        cfg = ValidationConfig()
        assert cfg.enabled is False
        assert cfg.test_command == "pytest tests/ -v --tb=short"
        assert cfg.test_timeout_seconds == 120
        assert cfg.fail_action == "warn"
        assert cfg.max_consecutive_failures == 3

    def test_validation_in_workflow_config(self) -> None:
        config = WorkflowConfig()
        assert config.validation.enabled is False

    def test_validation_custom_values(self) -> None:
        config = WorkflowConfig(
            validation={
                "enabled": True,
                "test_command": "python -m pytest -x",
                "test_timeout_seconds": 300,
                "fail_action": "inject",
                "max_consecutive_failures": 5,
            }
        )
        assert config.validation.enabled is True
        assert config.validation.test_command == "python -m pytest -x"
        assert config.validation.test_timeout_seconds == 300
        assert config.validation.fail_action == "inject"
        assert config.validation.max_consecutive_failures == 5

    def test_validation_rejects_bad_timeout(self) -> None:
        with pytest.raises(Exception):
            WorkflowConfig(validation={"test_timeout_seconds": 5})  # ge=10

    def test_validation_rejects_bad_max_failures(self) -> None:
        with pytest.raises(Exception):
            WorkflowConfig(validation={"max_consecutive_failures": 0})  # ge=1


class TestSessionRotationConfig:
    def test_session_rotation_defaults(self) -> None:
        from config import StagnationConfig
        cfg = StagnationConfig()
        assert cfg.session_max_turns == 200
        assert cfg.session_max_cost_usd == 20.0
        assert cfg.context_exhaustion_turn_threshold == 5
        assert cfg.context_exhaustion_window == 3

    def test_session_rotation_in_workflow_config(self) -> None:
        config = WorkflowConfig()
        assert config.stagnation.session_max_turns == 200
        assert config.stagnation.session_max_cost_usd == 20.0

    def test_session_rotation_custom(self) -> None:
        config = WorkflowConfig(
            stagnation={
                "session_max_turns": 100,
                "session_max_cost_usd": 10.0,
                "context_exhaustion_turn_threshold": 3,
                "context_exhaustion_window": 5,
            }
        )
        assert config.stagnation.session_max_turns == 100
        assert config.stagnation.session_max_cost_usd == 10.0
        assert config.stagnation.context_exhaustion_turn_threshold == 3
        assert config.stagnation.context_exhaustion_window == 5

    def test_session_rotation_validation(self) -> None:
        with pytest.raises(Exception):
            WorkflowConfig(stagnation={"session_max_turns": 5})  # ge=10

        with pytest.raises(Exception):
            WorkflowConfig(stagnation={"session_max_cost_usd": 0})  # gt=0

        with pytest.raises(Exception):
            WorkflowConfig(stagnation={"context_exhaustion_window": 1})  # ge=2
