from __future__ import annotations

from .model import ModelToolDefinition, ModelToolParameter


def native_world_tool_definitions() -> tuple[ModelToolDefinition, ...]:
    required_path = (
        ModelToolParameter("path", "Path relative to the execution world"),
    )
    required_session = (
        ModelToolParameter("session_id", "Persistent process session identifier"),
    )
    return (
        ModelToolDefinition("read_file", "Read a UTF-8 file", required_path),
        ModelToolDefinition(
            "write_file",
            "Write a UTF-8 file, creating parent directories",
            (
                ModelToolParameter("path", "Path relative to the execution world"),
                ModelToolParameter("content", "Complete file content"),
            ),
        ),
        ModelToolDefinition(
            "shell",
            "Run a shell command in the execution world",
            (
                ModelToolParameter("command", "Command to run"),
                ModelToolParameter(
                    "timeout_s",
                    "Timeout in seconds",
                    required=False,
                ),
            ),
        ),
        ModelToolDefinition(
            "glob",
            "List paths matching a glob pattern",
            (ModelToolParameter("pattern", "Glob pattern such as **/*.py"),),
        ),
        ModelToolDefinition(
            "search_text",
            "Find exact text in files",
            (
                ModelToolParameter("query", "Text to find"),
                ModelToolParameter(
                    "pattern",
                    "File glob to search",
                    required=False,
                ),
            ),
        ),
        ModelToolDefinition("file_metadata", "Inspect file metadata", required_path),
        ModelToolDefinition("git_status", "Show concise Git status"),
        ModelToolDefinition("git_diff_stat", "Show Git diff statistics"),
        ModelToolDefinition(
            "process_start",
            "Start a persistent process without a shell in the execution world",
            (
                ModelToolParameter("program", "Executable path or command name"),
                ModelToolParameter(
                    "arguments_json",
                    "JSON array of command arguments",
                    required=False,
                ),
                ModelToolParameter(
                    "cwd",
                    "Working directory relative to the execution world",
                    required=False,
                ),
                ModelToolParameter(
                    "environment_json",
                    "JSON object containing environment overrides",
                    required=False,
                ),
            ),
        ),
        ModelToolDefinition(
            "process_read",
            "Read new stdout and stderr chunks from a persistent process",
            required_session
            + (
                ModelToolParameter(
                    "cursor",
                    "Last output cursor already observed",
                    json_type="integer",
                    required=False,
                ),
            ),
        ),
        ModelToolDefinition(
            "process_write",
            "Write UTF-8 data to a persistent process stdin",
            required_session + (ModelToolParameter("data", "Data to write"),),
        ),
        ModelToolDefinition(
            "process_wait",
            "Wait up to a bounded timeout for a persistent process to exit",
            required_session
            + (
                ModelToolParameter(
                    "timeout_s",
                    "Timeout in seconds, at most 300",
                    json_type="number",
                    required=False,
                ),
            ),
        ),
        ModelToolDefinition(
            "process_terminate",
            "Terminate and reap a persistent process and its child processes",
            required_session
            + (
                ModelToolParameter(
                    "grace_s",
                    "Grace period before forced termination",
                    json_type="number",
                    required=False,
                ),
            ),
        ),
    )
