"""campaign: quests, worktrees, landing and a host job queue for multi-agent projects.

One module per subsystem (docs/source-layout.md lists them). A module imports only from those
before it in this order, so their imports never cycle: core, repo, mirrors, jobs, reuse, runner,
state, mail, journals, ci, evidence, reviews, campaign, leads, broadcast, disk, worker, map, title,
status, usage, wait, jobcmds, checks, land, quests, cli.
"""

from .cli import main

__all__ = ["main"]
