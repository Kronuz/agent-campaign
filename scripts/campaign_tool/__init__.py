"""campaign: quests, worktrees, landing and a host job queue for multi-agent projects.

State lives in .campaign/NAME/state.json in the campaign's home repository and is written only by
this tool, from that repository's main checkout. Jobs live in $CAMPAIGN_JOBS
(~/.scratch/campaign/jobs) on the host that runs them. `campaign help COMMAND` documents each
command; the skill's SKILL.md says when to use it. Each module holds one concern; a module imports
only from those below it, in this order: core, repo, jobs, procs, reads, reuse, mirrors, runner,
state, mail, journals, ci, evidence, reviews, campaign, leads, disk, worker, views, status, usage,
wait, jobcmds, checks, land, quests, cli.
"""

from .cli import main

__all__ = ["main"]
