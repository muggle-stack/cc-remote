"""Protocol-backed terminal actions; never call an engine directly."""

from __future__ import annotations

import json
import uuid

from cc_remote import protocol as p

# Explicit surface inventory. Wire additions do not silently become UI actions.
ACTION_CLASSES = (
    p.ListSessions,
    p.NewSession,
    p.DeleteSession,
    p.RenameSession,
    p.ArchiveSession,
    p.PinSession,
    p.ForkSession,
    p.ForkSessionWorktree,
    p.MigrateSession,
    p.RollbackSession,
    p.CompactSession,
    p.StartReview,
    p.Interrupt,
    p.Takeover,
    p.SetModel,
    p.SetEffort,
    p.SetAutoCompact,
    p.SetCodexContext,
    p.SetServiceTier,
    p.SetCollaborationMode,
    p.SetPerm,
    p.SetPermissionProfile,
    p.SetWebSearch,
    p.GetModels,
    p.GetPermissionProfiles,
    p.GetEngineCapabilities,
    p.ManageEnginePlugin,
    p.ManageEngineSkill,
    p.ManageEngineHook,
    p.GetContext,
    p.GetStatus,
    p.ConsumeRateLimitResetCredit,
    p.GetGoal,
    p.SetGoal,
    p.ClearGoal,
    p.DismissGoal,
    p.AcknowledgeCompletion,
    p.GetQueuedQuery,
    p.UpdateQueuedQuery,
    p.CancelQueuedQuery,
    p.ReorderQueuedQueries,
    p.OpenBtw,
    p.CloseBtw,
    p.GetDiff,
    p.GetTurnFileChanges,
    p.BrowseFiles,
    p.GetFilePreview,
    p.SaveMarkdown,
    p.GetPreviewAsset,
    p.AuthorizePreview,
    p.GetAgentDetail,
    p.GetHistoryImage,
    p.ListDir,
    p.GetWorkDashboard,
    p.GetWorkArtifacts,
    p.CreateWorkProject,
    p.DeleteWorkProject,
    p.AddWorkSource,
    p.DeleteWorkSource,
    p.CreateWorkPlugin,
    p.DeleteWorkPlugin,
    p.CreateWorkSchedule,
    p.DeleteWorkSchedule,
    p.DeleteWorkSession,
)
ACTIONS = {c.model_fields["type"].default: c for c in ACTION_CLASSES}
HIDDEN = set(p._Command.model_fields) | {"type", "request_id"}


def is_read(name: str) -> bool:
    return name.startswith(("get_", "list_")) or name == "browse_files"


def defaults(
    name: str, sid: str | None, engine: str, row: dict, presentation
) -> dict:
    result = {}
    for key, f in ACTIONS[name].model_fields.items():
        if key in HIDDEN and not (name == "authorize_preview" and key == "request_id"):
            continue
        if name == "authorize_preview" and key in {"request_id", "authorization_id"}:
            result[key] = presentation.reports.get(
                "preview_authorization_required", {}
            ).get(key, "")
        elif key == "session_id":
            result[key] = sid or ""
        elif key == "engine":
            # A live diff must not accidentally select the archive API, which
            # requires engine, turn and revision together.
            result[key] = None if name == "get_diff" else engine
        elif key == "space":
            result[key] = row.get("space", "code")
        elif key in {"claude_profile_id", "codex_profile_id"}:
            result[key] = row.get(key)
        elif key in {"last_turn_id", "checkpoint_id"}:
            selected = presentation.turns.get(
                presentation.selected_turn or presentation.active
            )
            result[key] = (
                getattr(
                    selected,
                    "fork_id" if key == "last_turn_id" else "checkpoint_id",
                    None,
                )
                if key != "checkpoint_id" or engine == "claude"
                else None
            )
        elif key in {"goal_id", "completion_id"}:
            result[key] = (
                presentation.goal_id
                if key == "goal_id"
                else presentation.completion.get("completion_id")
            )
        elif name == "set_goal" and key in {
            "objective",
            "status",
            "token_budget",
        }:
            result[key] = (presentation.goal or {}).get(
                "tokenBudget" if key == "token_budget" else key
            )
        elif key == "cwd":
            result[key] = row.get("cwd")
        elif name == "set_codex_context" and key == "max_context_tokens":
            result[key] = presentation.settings.get("codex_context", {}).get(
                "max_context_tokens"
            )
        elif not f.is_required():
            result[key] = f.get_default(call_default_factory=True)
        else:
            result[key] = ""
    return result


def build_action(name: str, payload: str, sid: str | None, client_id: str):
    if name not in ACTIONS:
        raise ValueError("Unknown action")
    if len(payload) > 2 * 1024 * 1024:
        raise ValueError("Action payload is too large")
    values = json.loads(payload)
    if not isinstance(values, dict):
        raise ValueError("Parameters must be a JSON object")
    hidden = HIDDEN - ({"request_id"} if name == "authorize_preview" else set())
    if set(values) & hidden:
        raise ValueError("Transport/routing fields cannot be edited")
    if "session_id" in values and values["session_id"] != sid:
        raise ValueError(
            "Session target is pinned; select another session first"
        )
    command_id = uuid.uuid4().hex
    values.update(sid=sid, client_id=client_id, cmd_id=command_id)
    cls = ACTIONS[name]
    sessionless = {
        "new_session",
        "list_sessions",
        "list_dir",
        "get_models",
        "get_engine_capabilities",
        "manage_engine_plugin",
        "manage_engine_skill",
        "manage_engine_hook",
        "get_work_dashboard",
        "create_work_project",
        "delete_work_project",
        "add_work_source",
        "delete_work_source",
        "create_work_plugin",
        "delete_work_plugin",
        "create_work_schedule",
        "delete_work_schedule",
    }
    if not sid and name not in sessionless:
        raise ValueError("Select a target session first")
    if "request_id" in cls.model_fields and name != "authorize_preview":
        values["request_id"] = command_id
    # Pydantic remains the one shared source of command validation.
    return cls(**values)
