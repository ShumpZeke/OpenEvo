from __future__ import annotations

from .kernel import Goal


def system_prompt() -> str:
    return (
        "You are OpenEvo's native autonomous agent. Work only inside the provided "
        "execution world. Inspect evidence before changing files, use structured "
        "tools for actions, verify the result, and finish with a concise outcome."
    )


def goal_prompt(goal: Goal, context: str) -> str:
    conditions = "\n".join(f"- {condition}" for condition in goal.success_conditions)
    sections = [f"Objective:\n{goal.objective}"]
    if conditions:
        sections.append(f"Success conditions:\n{conditions}")
    if context:
        sections.append(f"Selected context:\n{context}")
    return "\n\n".join(sections)
