"""Keep relay control-plane credentials out of model/tool subprocesses."""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
import sys
from collections.abc import Mapping


CONTROL_PLANE_SECRET_KEYS = (
    "WRAPPER_TOKEN",
    "WRAPPER_TOKENS_JSON",
    "LOGIN_PASSWORD",
    "LOGIN_USERS_JSON",
    "SESSION_SECRET",
)

# Ambient provider credentials would make every configured Claude profile use
# the wrapper service account regardless of its CLAUDE_CONFIG_DIR. The isolated
# Claude transport removes these keys from its copied child environment; each
# profile may repopulate the intended values through its own settings.json (or
# use native keychain auth).
CLAUDE_ACCOUNT_ENV_KEYS = (
    # Direct Anthropic API, subscription, and WIF selectors.
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_CONFIG_DIR",
    "ANTHROPIC_PROFILE",
    "ANTHROPIC_FEDERATION_RULE_ID",
    "ANTHROPIC_ORGANIZATION_ID",
    "ANTHROPIC_SERVICE_ACCOUNT_ID",
    "ANTHROPIC_IDENTITY_TOKEN_FILE",
    "ANTHROPIC_IDENTITY_TOKEN",
    "ANTHROPIC_WORKSPACE_ID",
    "CLAUDE_CODE_OAUTH_TOKEN",
    # Provider-specific model and request routing.
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_MODEL",
    "ANTHROPIC_DEFAULT_FABLE_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_SMALL_FAST_MODEL",
    "ANTHROPIC_SMALL_FAST_MODEL_AWS_REGION",
    "ANTHROPIC_BETAS",
    "ANTHROPIC_CUSTOM_HEADERS",
    "ANTHROPIC_CUSTOM_MODEL_OPTION",
    "ANTHROPIC_CUSTOM_MODEL_OPTION_NAME",
    "ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION",
    "ANTHROPIC_CUSTOM_MODEL_OPTION_SUPPORTED_CAPABILITIES",
    "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST",
    "CLAUDE_CODE_USE_ANTHROPIC_AWS",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_MANTLE",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_SKIP_ANTHROPIC_AWS_AUTH",
    "CLAUDE_CODE_SKIP_BEDROCK_AUTH",
    "CLAUDE_CODE_SKIP_MANTLE_AUTH",
    "CLAUDE_CODE_SKIP_VERTEX_AUTH",
    "CLAUDE_CODE_SKIP_FOUNDRY_AUTH",
    # Claude Platform on AWS and Amazon Bedrock credentials/routing.
    "ANTHROPIC_AWS_API_KEY",
    "ANTHROPIC_AWS_BASE_URL",
    "ANTHROPIC_AWS_WORKSPACE_ID",
    "ANTHROPIC_BEDROCK_BASE_URL",
    "ANTHROPIC_BEDROCK_MANTLE_BASE_URL",
    "ANTHROPIC_BEDROCK_REGION_PREFIX",
    "ANTHROPIC_BEDROCK_SERVICE_TIER",
    "AWS_BEARER_TOKEN_BEDROCK",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
    "AWS_PROFILE",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_SHARED_CREDENTIALS_FILE",
    "AWS_CONFIG_FILE",
    "AWS_ROLE_ARN",
    "AWS_ROLE_SESSION_NAME",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
    # Google Agent Platform credentials/routing.
    "ANTHROPIC_VERTEX_PROJECT_ID",
    "ANTHROPIC_VERTEX_BASE_URL",
    "CLOUD_ML_REGION",
    "GCLOUD_PROJECT",
    "GOOGLE_CLOUD_PROJECT",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "CLOUDSDK_CONFIG",
    "CLOUDSDK_AUTH_ACCESS_TOKEN",
    # Microsoft Foundry credentials/routing.
    "ANTHROPIC_FOUNDRY_RESOURCE",
    "ANTHROPIC_FOUNDRY_BASE_URL",
    "ANTHROPIC_FOUNDRY_API_KEY",
    "ANTHROPIC_FOUNDRY_AUTH_TOKEN",
    "AZURE_CLIENT_ID",
    "AZURE_TENANT_ID",
    "AZURE_CLIENT_SECRET",
    "AZURE_CLIENT_CERTIFICATE_PATH",
    "AZURE_FEDERATED_TOKEN_FILE",
    "AZURE_USERNAME",
    "AZURE_PASSWORD",
    "AZURE_AUTHORITY_HOST",
)


def sanitized_child_env(source: Mapping[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if source is None else source)
    for key in CONTROL_PLANE_SECRET_KEYS:
        env.pop(key, None)
    return env


def child_env_tombstones() -> dict[str, str]:
    """Claude SDK merges options.env over os.environ, so empty values scrub it."""
    return {key: "" for key in CONTROL_PLANE_SECRET_KEYS}


def _claude_config_override(
    config_dir: str, *, isolate_account_env: bool,
) -> str | None:
    # Claude keeps native account metadata in ~/.claude.json. Explicitly
    # setting CLAUDE_CONFIG_DIR=~/.claude instead selects ~/.claude/.claude.json
    # (and a different keychain identity), despite sharing settings/transcripts.
    # A profile rooted at the native directory must preserve that native layout.
    if isolate_account_env and (
        Path(config_dir).resolve(strict=False)
        == (Path.home() / ".claude").resolve(strict=False)
    ):
        return None
    return config_dir


def claude_profile_child_env(
    config_dir: str | None,
    *,
    isolate_account_env: bool,
) -> dict[str, str]:
    """Build the request-local part of an isolated Claude SDK environment.

    The pinned Agent SDK merges this mapping over ``os.environ``.  An empty
    credential value is therefore not an unset operation: current Claude auth
    treats it as a selected-but-invalid credential source. The isolated SDK
    transport removes inherited account selectors from its own environment
    copy, while this overlay selects the profile's native config root.
    """
    if isolate_account_env and config_dir is None:
        raise ValueError("isolated Claude profile requires a config directory")
    result = child_env_tombstones()
    if config_dir is not None:
        override = _claude_config_override(
            config_dir, isolate_account_env=isolate_account_env)
        if override is not None:
            result["CLAUDE_CONFIG_DIR"] = override
    return result


def claude_sdk_process_env(
    overlay: Mapping[str, str],
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build one exact Claude child environment without mutating its parent."""
    result = dict(os.environ if source is None else source)
    result.pop("CLAUDECODE", None)
    # An omitted profile override means Claude's native layout, never an
    # unrelated account directory inherited from the Wrapper's own process.
    result.pop("CLAUDE_CONFIG_DIR", None)
    for key in CLAUDE_ACCOUNT_ENV_KEYS:
        result.pop(key, None)
    result.update(overlay)
    return result


def claude_profile_process_env(
    config_dir: str | None,
    *,
    isolate_account_env: bool,
) -> dict[str, str]:
    """Build a complete environment for a directly spawned Claude CLI.

    Unlike ``ClaudeAgentOptions.env``, ``subprocess`` does not merge an env
    mapping over the parent. Preserve ordinary runtime variables while
    removing control-plane credentials and any ambient account selectors that
    could override the requested config directory.
    """
    result = sanitized_child_env()
    if isolate_account_env:
        result.pop("CLAUDE_CONFIG_DIR", None)
        for key in CLAUDE_ACCOUNT_ENV_KEYS:
            result.pop(key, None)
    if config_dir is not None:
        override = _claude_config_override(
            config_dir, isolate_account_env=isolate_account_env)
        if override is not None:
            result["CLAUDE_CONFIG_DIR"] = override
    return result


def scrub_parent_control_secrets() -> None:
    """Hide captured credentials from model/tool descendants.

    On Linux, deleting a key from ``os.environ`` does not rewrite the initial
    environment bytes exposed through ``/proc/<pid>/environ``.  Claude tools run
    as the same Unix user as this process, so make the wrapper non-dumpable
    before removing the live mapping.  This also blocks ptrace/process-memory
    reads of the in-memory transport token.  Failing to apply the protection is
    fatal: running a bypass-permissions model with a readable bearer token would
    be a privilege-boundary bypass.
    """
    if sys.platform.startswith("linux"):
        libc = ctypes.CDLL(None, use_errno=True)
        prctl = libc.prctl
        prctl.argtypes = [
            ctypes.c_int,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
        ]
        prctl.restype = ctypes.c_int
        # linux/prctl.h: PR_SET_DUMPABLE = 4.
        if prctl(4, 0, 0, 0, 0) != 0:
            error_number = ctypes.get_errno()
            raise OSError(
                error_number,
                "failed to disable process dumpability for wrapper secrets",
            )
    for key in CONTROL_PLANE_SECRET_KEYS:
        os.environ.pop(key, None)
