# Maintenance

A maintenance assignment makes one mechanical change across repositories (pins, renames, CI repair).

- Make exactly the change the prompt names, everywhere it applies, and nothing else: no fixes on
  the way.
- One commit per repository, its CI green before the next.
- Stop at the first surprise (a conflict, red CI you did not cause, a change that turns out not to
  be mechanical, a repository that is not where the prompt says) and report it.
