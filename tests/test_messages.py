"""Messages between leads, contributors and campaigns, and the home they share."""

from __future__ import annotations

import json
import time

from harness import CampaignCase


class SharedHome(CampaignCase):
    def test_one_home_holds_two_campaigns_apart(self) -> None:
        self.ok(self.home, "init", "--name", "perf")
        self.fails(self.home, "status", match="pass --campaign NAME")
        for name in ("demo", "perf"):
            self.ok(
                self.home,
                "new",
                "fix",
                "--title",
                name,
                "--risk",
                "low",
                "--files",
                "app.py",
                "--campaign",
                name,
                "--check",
                "unit=true",
            )
            started = self.ok(self.home, "start", "fix", "--campaign", name)
        self.assertIn("may overlap demo:fix", started.err)  # neighbors in one home still see each other
        self.assertEqual(self.quest("fix")["branch"], "quest/demo/fix")
        perf = self.root / "app-wt" / "perf" / "fix"
        self.assertTrue((perf / ".campaign/perf/journal/fix.md").exists())
        # a quest's worktree knows its campaign; the main checkout needs the name
        self.assertIn("fix  active  low  perf", self.ok(perf, "status").out)
        self.env["CAMPAIGN"] = "perf"
        self.assertIn("campaign perf", self.ok(self.home, "status").out)
        self.env.pop("CAMPAIGN")
        # each lead's wait sees only its campaign's events
        for name in ("demo", "perf"):
            self.run_cli(self.home, "wait", "--commits", "--timeout", "1", "--campaign", name)
        self.commit(perf, {"app.py": "print('perf')\n"})
        self.assertEqual(self.run_cli(self.home, "wait", "--commits", "--timeout", "1", "--campaign", "demo").code, 3)
        self.assertIn("quest/perf/fix moved", self.ok(self.home, "wait", "--commits", "--campaign", "perf").out)


class SharedHomeMessages(CampaignCase):
    def test_messages_and_outboxes_stay_apart_in_a_shared_home(self) -> None:
        self.ok(self.home, "init", "--name", "perf")
        for name in ("demo", "perf"):
            self.ok(
                self.home,
                "new",
                "fix",
                "--title",
                name,
                "--risk",
                "low",
                "--files",
                "app.py",
                "--campaign",
                name,
                "--check",
                "unit=true",
            )
            self.ok(self.home, "start", "fix", "--campaign", name)
        demo, perf = self.wt("fix"), self.root / "app-wt" / "perf" / "fix"
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        self.ok(elsewhere, "message", "demo:fix", "for demo", "--from", "a")
        self.ok(elsewhere, "message", "perf:fix", "for perf", "--from", "b")
        self.ok(perf, "message", "fix", "from its own worktree", "--from", "c")  # the worktree knows its campaign
        self.fails(self.home, "message", "fix", "which?", match="pass --campaign NAME or set CAMPAIGN; or address")
        self.assertIn("msg-1 from a: for demo", self.ok(demo, "brief").out)
        self.assertNotIn("for perf", self.ok(demo, "brief").out)
        seen = self.ok(perf, "brief").out
        self.assertIn("msg-1 from b: for perf", seen)
        self.assertIn("msg-2 from c: from its own worktree", seen)
        self.assertNotIn("for demo", seen)
        # starting perf:fix told demo's lead, whose work it overlaps, and nobody else
        self.assertIn(
            "for the lead, from campaign perf: perf:fix started",
            self.ok(self.home, "status", "--campaign", "demo").out,
        )
        self.assertNotIn("for the lead", self.ok(self.home, "status", "--campaign", "perf").out)
        # each campaign's outbox is its own ref
        bare = self.root / "remote.git"
        self.git(self.root, "init", "-q", "--bare", str(bare))
        self.git(self.home, "remote", "add", "origin", str(bare))
        for name in ("demo", "perf"):
            self.ok(self.home, "config", f"peers=far={bare}", "--campaign", name)
            self.ok(self.home, "message", "far:x", f"from {name}", "--campaign", name)
        remote = ("--git-dir", str(bare))
        refs = self.git(self.root, *remote, "for-each-ref", "--format=%(refname)", "refs/campaign").split()
        self.assertEqual(refs, ["refs/campaign/outbox/demo", "refs/campaign/outbox/perf"])
        for name in ("demo", "perf"):
            files = self.git(self.root, *remote, "ls-tree", "-r", "--name-only", f"refs/campaign/outbox/{name}").split()
            self.assertEqual(len(files), 1)


class Messages(CampaignCase):
    def test_an_agent_outside_the_home_names_it(self) -> None:
        wt = self.started("fix", files="app.py")
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        unrelated = self.repo("unrelated", {"x.txt": "x\n"})  # a repository that holds no campaign
        self.assertTrue(self.ok(elsewhere, "-C", str(self.home), "brief").out.startswith("campaign demo:"))
        self.assertIn("fix active low", self.ok(unrelated, "brief", "--campaign", "demo").out)
        self.ok(elsewhere, "-C", str(self.home), "set", "fix", "title=renamed")  # writes too
        self.assertEqual(self.quest("fix")["title"], "renamed")
        self.env["CAMPAIGN_HOME"] = str(self.home)
        self.ok(elsewhere, "set", "fix", "risk=normal")
        self.assertEqual(self.quest("fix")["risk"], "normal")
        del self.env["CAMPAIGN_HOME"]
        self.fails(wt, "set", "fix", "risk=high", match="run this there, or with -C")  # an contributor never writes
        self.fails(elsewhere, "brief", "--campaign", "nope", match="no campaign nope here or on this machine")
        self.fails(elsewhere, "brief", match="pass -C DIR, --campaign NAME, or set CAMPAIGN_HOME")

    def test_check_and_wait_tell_the_owner_of_messages_a_lead_has_not_received(self) -> None:
        inbox, calls, fake = self.root / "inbox.json", self.root / "cmux-calls", self.root / "bin" / "cmux"
        script = f'printf "%s|" "$@" >> {calls}; echo >> {calls}\n[ "$1 $2" = "agent inbox" ] && cat {inbox}\n'
        fake.write_text("#!/bin/sh\n" + script + "exit 0\n")
        fake.chmod(0o755)
        now = time.time()

        def messages(*rows: tuple[str, float]) -> None:  # the queued ones cmux lists
            inbox.write_text(json.dumps({"messages": [{"id": m, "created_at": now - n * 60} for m, n in rows]}))

        messages(("m-old", 20), ("m-new", 2))
        (self.root / "leads").mkdir(exist_ok=True)
        (self.root / "leads" / "other").write_text(json.dumps({"surface": "surface-other", "workspace": "ws"}))
        self.env["CMUX_WORKSPACE_ID"] = "ws-demo"
        out = self.ok(self.home, "check").out
        self.assertIn("1 message to other's lead (cmux surface surface-other) undelivered for up to 20m", out)
        self.assertIn("agent|inbox|--surface|surface-other|--state|queued|--json|", calls.read_text())
        self.assertEqual(calls.read_text().count("notify|--title|"), 1)
        self.ok(self.home, "check")
        self.assertEqual(calls.read_text().count("notify|--title|"), 1, "the owner was told twice of one message")
        messages(("m-old", 20), ("m-two", 16))
        r = self.ok(self.home, "wait", self.submit(self.home, "--", "true"), "--timeout", "60")
        self.assertIn("2 messages to other's lead", r.err)
        self.assertEqual(calls.read_text().count("notify|--title|"), 2)
        fake.write_text("#!/bin/sh\nexit 1\n")
        self.assertNotIn("lead (cmux surface", self.ok(self.home, "check").out, "a failing cmux is not silent")

    def test_a_leads_message_shows_until_it_is_closed(self) -> None:
        wt = self.started("fix", files="app.py")
        self.run_cli(self.home, "wait", "--commits", "--timeout", "1")
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        sent = self.ok(elsewhere, "message", "demo:", "the CI runner moved", "--from", "owner").out
        self.assertIn("msg-1 for the lead", sent)
        woke = self.ok(self.home, "wait", "--commits", "--timeout", "30").out
        self.assertIn("demo: msg-1 for the lead, from owner: the CI runner moved", woke)
        self.assertIn("msg-1 for the lead, from owner: the CI runner moved", self.ok(self.home, "status").out)
        self.assertIn("- msg-1 for the lead, from owner: the CI runner moved", self.ok(self.home, "brief").out)
        self.assertNotIn("CI runner", self.ok(wt, "brief").out)  # an contributor's brief is its quest's
        self.commit(wt, {"app.py": "print(2)\n"})
        self.verified("fix")
        self.ok(self.home, "land", "fix")  # a lead's message blocks no quest
        self.assertIn("msg-1 for the lead", self.ok(self.home, "brief").out)
        self.fails(self.home, "message", ":", "--close", "msg-1", match="needs --reason")
        self.ok(self.home, "message", ":", "--close", "msg-1", "--reason", "updated the runbook")
        self.assertNotIn("CI runner", self.ok(self.home, "status").out)
        self.assertEqual(self.state()["messages"][0]["closed"], "updated the runbook")

    def test_messages_cross_machines_through_outboxes(self) -> None:
        self.started("fix", files="app.py")
        self.run_cli(self.home, "wait", "--commits", "--timeout", "1")
        # another machine: its own job queue and registry, and a repository its outbox is pushed to
        bare = self.root / "far.git"
        self.git(self.root, "init", "-q", "--bare", str(bare))
        far = self.repo("far", {"f.py": "1\n"})
        self.git(far, "remote", "add", "origin", str(bare))
        here = self.env["CAMPAIGN_JOBS"]
        self.env["CAMPAIGN_JOBS"] = str(self.root / "far-machine" / "jobs")  # its registry is beside it
        self.ok(far, "init", "--name", "far")
        self.ok(far, "config", f"peers=demo={self.root / 'demo.git'}")
        sent = self.ok(far, "message", "demo:fix", "the encoder drops the last byte", "--from", "explorer").out
        self.assertIn("queued in far's outbox", sent)
        self.fails(far, "message", "nowhere:x", "text", match="no campaign nowhere on this machine or among the peers")
        self.env["CAMPAIGN_JOBS"] = here
        self.ok(self.home, "config", f"peers=far={bare}")
        self.env["CAMPAIGN_PEER_SECONDS"] = "0"
        woke = self.ok(self.home, "wait", "--commits", "--timeout", "30").out
        self.assertIn("fix: msg-1 from far: explorer: the encoder drops the last byte", woke)
        self.assertIn("unanswered messages: msg-1", self.ok(self.home, "brief").out)  # it gates like a local note
        self.assertEqual(self.run_cli(self.home, "wait", "--commits", "--timeout", "2").code, 3)  # fetched once

    def test_a_message_from_outside_wakes_the_game_master_and_must_be_answered_to_land(self) -> None:
        wt = self.started("fix", files="app.py")
        self.commit(wt, {"app.py": "print(2)\n"})
        self.verified("fix")
        self.run_cli(self.home, "wait", "--commits", "--timeout", "1")  # the lead has seen it all
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        r = self.ok(elsewhere, "message", "demo:fix", "util rounds wrong past 2^53", "--from", "explorer hunt-lib")
        self.assertIn("msg-1 for fix", r.out)
        woke = self.ok(self.home, "wait", "--commits", "--timeout", "30").out
        self.assertIn("fix: msg-1 from explorer hunt-lib: util rounds wrong past 2^53", woke)
        self.assertIn("msg-1 from explorer hunt-lib", self.ok(self.home, "status").out)
        self.fails(self.home, "land", "fix", match="unanswered messages: msg-1")
        self.commit(wt, {".campaign/demo/journal/fix.md": "msg-1: util.py is outside this quest; no change\n"})
        self.assertNotIn("msg-1 from", self.ok(self.home, "status").out)  # answered
        self.ok(self.home, "land", "fix")
        filed = self.quest("fix")["messages"]
        self.assertEqual([(n["id"], n["by"]) for n in filed], [("msg-1", "explorer hunt-lib")])
        self.assertTrue(filed[0]["answered"])
        self.fails(elsewhere, "message", "demo:fix", "late", match="fix is landed")

    def test_the_game_master_closes_a_message_with_a_reason(self) -> None:
        self.started("fix", files="app.py")
        self.ok(self.home, "message", "fix", "cache the parse", "--from", "owner")
        self.fails(self.home, "message", "fix", "--close", "msg-1", match="needs --reason")
        self.ok(self.home, "message", "fix", "--close", "msg-1", "--reason", "measured: no gain")
        self.assertNotIn("msg-1", self.ok(self.home, "status").out)
        self.assertEqual(self.quest("fix")["messages"][0]["closed"], "measured: no gain")
        self.assertIn("msg-2 for fix", self.ok(self.home, "message", "fix", "another").out)  # ids are not reused
