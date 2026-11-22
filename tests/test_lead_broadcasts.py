"""Broadcasts to other campaigns' leads: the skill's own changes, exactly once each, and `message --all`."""

from __future__ import annotations

import json
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

from harness import SKILL, CampaignCase

DIE_AT_RECORD = """
import os, signal, sys
sys.path.insert(0, sys.argv[1])
from campaign_tool import broadcast
real, writes = broadcast.write_json, [0]
def dying(path, data):
    if path.name != "seen.json":  # a receiver's record: what it was last told
        writes[0] += 1
        if writes[0] == int(sys.argv[2]):
            os.kill(os.getpid(), signal.SIGKILL)  # sent, not recorded
    real(path, data)
broadcast.write_json = dying
broadcast.announce_skill()
"""


class LeadBroadcasts(CampaignCase):
    def setUp(self) -> None:
        super().setUp()
        # the skill's repository, the sender's clone it lands through, and the install
        self.origin = self.root / "agent-campaign.git"
        self.git(self.root, "init", "-q", "--bare", "-b", "main", str(self.origin))
        seed = self.repo("seed", {"SKILL.md": "skill\n"})
        self.git(seed, "push", "-q", str(self.origin), "main")
        self.clone = self.root / "agent-campaign"
        self.skill = self.root / "skill"
        for where in (self.clone, self.skill):
            self.git(self.root, "clone", "-q", str(self.origin), str(where))
        self.env["CAMPAIGN_SKILL"] = str(self.skill)
        # demo lands the skill's changes (a quest of it works in the clone); one and two only read them
        quest = ("--title", "t", "--risk", "low", "--files", "SKILL.md", "--check", "unit=true")
        self.ok(self.home, "new", "tool", *quest, "--repo", "../agent-campaign")
        self.others = {name: self.repo(name, {"x.py": "1\n"}) for name in ("one", "two")}
        for name, home in self.others.items():
            self.ok(home, "init", "--name", name)

    def lead_mail(self, home: Path, name: str) -> list[dict[str, Any]]:
        folder = home / ".git" / "campaign-mail" / name / "@"
        return [json.loads(f.read_text()) for f in sorted(folder.glob("msg-*.json"))]

    def all_mail(self) -> dict[str, list[str]]:
        homes = {"demo": self.home, **self.others}
        return {name: [m["text"] for m in self.lead_mail(home, name)] for name, home in homes.items()}

    def land(self, subject: str, body: str = "") -> str:
        self.commit(self.clone, {"SKILL.md": subject + "\n"}, subject + (f"\n\n{body}" if body else ""))
        self.git(self.clone, "push", "-q", "origin", "main")  # moves the clone's origin/main: landed
        return self.git(self.clone, "rev-parse", "--short", "HEAD")

    def install(self) -> None:
        self.git(self.skill, "pull", "-q", "--ff-only")

    def test_a_landed_commit_then_its_install_reach_each_other_lead_once(self) -> None:
        self.ok(self.home, "status")  # the first look records a baseline and tells nobody
        self.assertEqual(self.all_mail(), {"demo": [], "one": [], "two": []})
        short = self.land("Teach leads the broadcast", "Leads: a skill change reaches you by message")
        self.ok(self.others["one"], "status")  # any command, in any campaign, announces it
        mail = self.all_mail()
        self.assertEqual(mail["demo"], [])  # the sender lands it, so it is never told
        for name in self.others:
            self.assertEqual(len(mail[name]), 1, mail)
            text = mail[name][0]
            self.assertIn(
                f"1 commit landed on agent-campaign's main, not installed here yet: agent-campaign:{short}", text
            )
            self.assertIn("Teach leads the broadcast (for leads: a skill change reaches you by message)", text)
        self.ok(self.home, "status")
        self.ok(self.others["two"], "brief")
        self.assertEqual(self.all_mail(), mail)  # a second and third run send nothing
        self.install()
        self.ok(self.home, "status")
        mail = self.all_mail()
        self.assertEqual(mail["demo"], [])
        for name in self.others:
            self.assertEqual(len(mail[name]), 2, mail)
            self.assertIn(
                f"campaign skill: agent-campaign:{short} installed here, 1 commit: agent-campaign:{short}",
                mail[name][1],
            )
        self.ok(self.others["one"], "status")
        self.assertEqual(self.all_mail(), mail)

    def test_a_kill_between_sending_and_recording_neither_drops_nor_repeats(self) -> None:
        self.ok(self.home, "status")

        def killed_then_rerun(kill_at: int, sent: list[int], told: int) -> None:
            died = subprocess.run(
                [sys.executable, "-c", DIE_AT_RECORD, str(SKILL / "scripts"), str(kill_at)], cwd=self.home, env=self.env
            )
            self.assertEqual(died.returncode, -signal.SIGKILL)
            mail = self.all_mail()
            self.assertEqual([len(mail[n]) for n in ("demo", "one", "two")], [0, *sent], mail)  # sent, then killed
            self.ok(self.home, "status")  # the rerun
            mail = self.all_mail()
            self.assertEqual([len(mail[n]) for n in ("demo", "one", "two")], [0, told, told], mail)
            self.assertEqual([len(set(mail[n])) for n in ("one", "two")], [told, told], mail)

        self.land("First change")
        killed_then_rerun(1, [1, 0], 1)  # one told, not recorded: the rerun records it and tells two
        self.land("Second change")
        killed_then_rerun(2, [2, 2], 2)  # both told, two not recorded
        self.install()
        killed_then_rerun(1, [3, 2], 3)  # the install, as the landings

    def test_a_closed_notice_is_not_sent_again(self) -> None:
        self.ok(self.home, "status")
        self.land("A change")
        died = subprocess.run(
            [sys.executable, "-c", DIE_AT_RECORD, str(SKILL / "scripts"), "1"], cwd=self.home, env=self.env
        )
        self.assertEqual(died.returncode, -signal.SIGKILL)
        skill, self.env["CAMPAIGN_SKILL"] = self.env["CAMPAIGN_SKILL"], ""  # closed before any rerun
        self.ok(self.others["one"], "message", ":", "--close", "msg-1", "--reason", "read")
        self.env["CAMPAIGN_SKILL"] = skill
        self.ok(self.home, "status")
        self.assertEqual(self.all_mail()["one"], [])  # its record in the state still names it
        self.assertEqual(len(self.all_mail()["two"]), 1)

    def test_only_the_installed_checkout_announces(self) -> None:
        self.ok(self.home, "status")
        self.land("A change")
        del self.env["CAMPAIGN_SKILL"]  # this checkout, a quest's, is not the `campaign` on PATH
        self.env["PATH"] = f"{self.root / 'bin'}:/usr/bin:/bin"
        self.ok(self.home, "status")
        self.assertEqual(self.all_mail(), {"demo": [], "one": [], "two": []})

    def test_landed_commits_reach_the_senders_peers_through_its_outbox_once(self) -> None:
        bare = self.root / "demo-outbox.git"
        self.git(self.root, "init", "-q", "--bare", str(bare))
        self.git(self.home, "remote", "add", "origin", str(bare))
        self.ok(self.home, "config", "peers=far=/nowhere,one=/nowhere")  # one is on this machine: told here
        self.ok(self.home, "status")
        self.land("A change")
        self.ok(self.home, "status")
        self.install()
        self.ok(self.home, "status")
        files = self.git(self.root, "--git-dir", str(bare), "ls-tree", "-r", "--name-only", "refs/campaign/outbox/demo")
        self.assertEqual(len(files.split()), 1, files)  # far: the landing; its own machine announces its install
        self.assertTrue(files.startswith("far/demo-skill-landed-"), files)
        self.assertEqual(len(self.all_mail()["one"]), 2)


class Broadcast(CampaignCase):
    def test_message_all_reaches_every_other_lead_once(self) -> None:
        others = {name: self.repo(name, {"x.py": "1\n"}) for name in ("one", "two")}
        for name, home in others.items():
            self.ok(home, "init", "--name", name)
        bare = self.root / "demo-outbox.git"
        self.git(self.root, "init", "-q", "--bare", str(bare))
        self.git(self.home, "remote", "add", "origin", str(bare))
        self.ok(self.home, "config", "peers=far=/nowhere,one=/nowhere")  # one twice: here and as a peer
        sent = self.ok(self.home, "message", "--all", "rule: never hand-list receivers", "--from", "lead").out
        self.assertIn("msg-1 for one's lead", sent)
        self.assertIn("msg-1 for two's lead", sent)
        self.assertIn("for far's lead, in demo's outbox", sent)
        again = self.ok(self.home, "message", "--all", "rule: never hand-list receivers").out
        self.assertIn("one's lead holds this message open already", again)
        for name, home in others.items():
            mail = [json.loads(f.read_text()) for f in (home / ".git/campaign-mail" / name / "@").glob("*.json")]
            self.assertEqual(
                [(m["text"], m["by"], m["origin"]) for m in mail],
                [("rule: never hand-list receivers", "demo: lead", "demo")],
            )
        self.assertFalse((self.home / ".git/campaign-mail/demo/@").exists())  # never the sender
        files = self.git(self.root, "--git-dir", str(bare), "ls-tree", "-r", "--name-only", "refs/campaign/outbox/demo")
        self.assertEqual([f.partition("/")[0] for f in files.split()], ["far"])  # nor to a peer
        self.fails(self.home, "message", "--all", "one:", "text", match="--all takes only the message's TEXT")
