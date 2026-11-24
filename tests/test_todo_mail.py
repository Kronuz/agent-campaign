"""The lead's to-do as mail: the worker leaves it in the lead's inbox and wakes the lead, repeats an
undone item, and tells the owner of one left undone for hours. The clock is the `now` each look is
given; cmux is a stub that lists the lead's surface and records every call."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from typing import Any
from unittest import mock

from harness import CAMPAIGN, CampaignCase, campaign_module

TREE = {"windows": [{"workspaces": [{"id": "ws-lead", "panes": [{"surfaces": [{"id": "surface-lead"}]}]}]}]}


class TodoMail(CampaignCase):
    def setUp(self) -> None:
        super().setUp()
        self.calls = self.root / "cmux-calls"
        fake = self.root / "bin" / "cmux"
        fake.write_text(
            "#!/bin/sh\n"
            f"printf '%s\\037' \"$CMUX_WORKSPACE_ID\" \"$@\" >> {self.calls}; printf '\\036' >> {self.calls}\n"
            f"case \"$*\" in *tree*) echo '{json.dumps(TREE)}';; esac\n"
        )
        fake.chmod(0o755)
        (self.root / "leads").mkdir(exist_ok=True)
        (self.root / "leads" / "demo").write_text(json.dumps({"surface": "surface-lead"}) + "\n")
        self.tool: Any = None
        self.t0 = time.time()

    def new(self, *pids: str) -> None:
        for pid in pids:
            self.ok(self.home, "new", pid, "--title", pid, "--risk", "low", "--files", f"{pid}.py")

    def look(self, at: float) -> None:
        """One worker tick AT seconds after the test began (the fake clock)."""
        with mock.patch.dict(os.environ, self.env, clear=True):
            if self.tool is None:
                self.tool = campaign_module()
            self.tool.todo.lead_todo(self.tool.all_jobs(), self.t0 + at)

    def mail(self) -> list[dict[str, Any]]:
        """Every message the lead's mailbox holds, closed ones included, oldest first."""
        folder = self.home / ".git" / "campaign-mail" / "demo" / "@"
        found = [json.loads(f.read_text()) for f in folder.glob("msg-*.json")] if folder.is_dir() else []
        return sorted(found, key=lambda m: int(m["id"].partition("-")[2]))

    def todo_mail(self) -> list[dict[str, Any]]:
        return [m for m in self.mail() if m["by"] == "campaign" and "todo" in (m.get("settles") or {})]

    def cmux(self, what: str) -> list[list[str]]:
        """The stub's calls whose command is WHAT: [workspace, *args] each."""
        calls = self.calls.read_text().split("\036")[:-1] if self.calls.exists() else []
        rows = [x.split("\037")[:-1] for x in calls]
        return [r for r in rows if r[1:2] == what.split()[:1] and " ".join(r[1:]).startswith(what)]

    def journal(self, pid: str, state: str, blocked: str = "", ago: float = 0) -> None:
        """Sets PID's journal State (and Blocked on), as written AGO seconds before now."""
        path = self.wt(pid) / f".campaign/demo/journal/{pid}.md"
        text = re.sub(r"^State:.*$", f"State: {state}", path.read_text(), count=1, flags=re.M)
        if blocked:
            text = re.sub(r"(## Blocked on\n\n)<[^\n]*>", rf"\g<1>{blocked}", text, count=1)
        path.write_text(text)
        if ago:
            os.utime(path, (time.time() - ago,) * 2)

    # A1: a launchable quest reaches the lead as mail within a tick, with no lead command run

    def test_a_launchable_quest_is_mailed_to_the_lead_within_a_tick(self) -> None:
        self.new("top")
        self.look(0)
        (m,) = self.todo_mail()
        self.assertEqual(m["by"], "campaign")
        self.assertIn("`campaign start top`", m["text"])
        (woke,) = self.cmux("agent message surface-lead")
        self.assertIn("`campaign start top`", woke[-1])

    def test_the_worker_mails_the_lead_with_no_job_queued(self) -> None:
        self.new("top")
        worker = subprocess.Popen(
            [sys.executable, str(CAMPAIGN), "worker"],
            cwd=self.root,  # not the home: no lead command runs
            env=self.env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            self.wait_until(lambda: bool(self.todo_mail()), timeout=30)
            self.assertIsNone(worker.poll(), "a worker stays while its lead lives")
        finally:
            worker.kill()
            worker.wait()
        self.assertIn("`campaign start top`", self.todo_mail()[0]["text"])

    def test_a_campaign_with_no_live_lead_gets_no_mail(self) -> None:
        self.new("top")
        (self.root / "leads" / "demo").write_text(json.dumps({"surface": "surface-gone"}) + "\n")
        self.look(0)
        self.assertEqual(self.todo_mail(), [])
        (self.root / "leads" / "demo").write_text(json.dumps({"surface": "surface-lead"}) + "\n")
        self.look(60)  # the next lead gets the items that waited
        self.assertEqual(len(self.todo_mail()), 1)

    # A2: an undone item repeats after the interval, whatever was closed; a done one never again

    def test_an_undone_item_repeats_until_its_quest_starts(self) -> None:
        self.new("top")
        self.look(0)
        first = self.todo_mail()[0]["id"]
        self.ok(self.home, "message", ":", "--close", first, "--reason", "read it")
        self.look(900)
        self.assertEqual(len(self.cmux("agent message")), 1, "nothing again inside the interval")
        self.look(1800)
        again = self.todo_mail()
        self.assertEqual(len(again), 1, "the closed one is gone; the repeat is new")
        self.assertNotEqual(again[0]["id"], first)
        self.assertIn("`campaign start top`", again[0]["text"])
        self.assertEqual(len(self.cmux("agent message")), 2)
        self.ok(self.home, "start", "top")
        for at in (3600, 5400, 9000):
            self.look(at)
        later = [m for m in self.todo_mail() if m["id"] != again[0]["id"]]
        self.assertFalse([m for m in later if "campaign start top" in m["text"]], later)
        with mock.patch.dict(os.environ, self.env, clear=True):
            c = self.tool.Campaign(self.home, "demo")
            self.assertFalse([m for m in self.tool.inbox(c, "") if "campaign start top" in m["text"]])
        self.ok(self.home, "config", "suite=python3 app.py")  # a save records the settled ones
        kept = [m for m in self.state().get("messages", []) if m["by"] == "campaign" and m["id"] != first]
        self.assertIn(again[0]["id"], [m["id"] for m in kept])
        self.assertFalse([m for m in kept if "text" in m], "a settled to-do's id stays, not its text")

    # A3: items due together go out in one message; an unchanged list inside the interval sends nothing

    def test_items_due_together_go_out_as_one_message(self) -> None:
        self.new("a", "b", "c")
        self.look(0)
        (m,) = self.todo_mail()
        self.assertEqual(len([x for x in m["text"].splitlines() if x.startswith("todo: start ")]), 3, m["text"])
        self.assertEqual(len(self.cmux("agent message")), 1)
        for at in (300, 900):
            self.look(at)
        self.assertEqual(len(self.todo_mail()), 1, "an unchanged list inside the interval sends nothing")
        self.assertEqual(len(self.cmux("agent message")), 1)
        self.new("d")
        self.look(1200)
        last = self.todo_mail()[-1]
        self.assertEqual(len([x for x in last["text"].splitlines() if x.startswith("todo: start ")]), 4)
        with mock.patch.dict(os.environ, self.env, clear=True):
            c = self.tool.Campaign(self.home, "demo")
            self.assertEqual([x["id"] for x in self.tool.inbox(c, "")], [last["id"]], "the newer list replaces it")

    def test_the_worker_looks_at_a_campaign_at_most_every_five_minutes(self) -> None:
        self.new("a")
        self.look(0)
        self.new("b")
        self.look(100)
        self.look(250)
        self.assertEqual(len(self.todo_mail()), 1)
        self.look(301)
        self.assertEqual(len(self.todo_mail()), 2)

    # todo-mail-trust A1: the standing mail matches the list now; an age alone never resends it

    def test_an_item_that_leaves_replaces_the_mail_at_the_next_look(self) -> None:
        self.new("a", "b")
        self.look(0)
        self.ok(self.home, "start", "a")
        self.look(300)
        mails = self.todo_mail()
        self.assertEqual(len(mails), 2, "replaced at the next look, not at the repeat")
        self.assertNotIn("campaign start a", mails[-1]["text"])
        self.assertIn("`campaign start b`", mails[-1]["text"])
        with mock.patch.dict(os.environ, self.env, clear=True):
            c = self.tool.Campaign(self.home, "demo")
            self.assertEqual([x["id"] for x in self.tool.inbox(c, "")], [mails[-1]["id"]])
        self.look(600)
        self.assertEqual(len(self.todo_mail()), 2, "an unchanged list sends nothing")

    def test_a_changed_gap_replaces_the_mail_at_the_next_look(self) -> None:
        self.env["CAMPAIGN_IDLE"] = "0"  # no contributor at work: its finish is the lead's
        self.started("fin", files="fin.py", check="unit=true")
        brief = self.home / ".campaign/demo/quests/fin.md"
        brief.write_text(brief.read_text().replace("None in this test.", "- A1 one: a test\n- A2 two: a test"))
        self.git(self.home, "commit", "-qam", "criteria")
        self.journal("fin", "ready for review")
        self.look(0)
        (first,) = self.todo_mail()
        self.assertRegex(first["text"], r"todo: finish fin .*A1", first["text"])
        jid = self.submit(self.wt("fin"), "--quest", "fin", "--for", "A1", "--", "true")
        self.ok(self.home, "wait", jid, "--timeout", "60")
        self.look(300)
        mails = self.todo_mail()
        self.assertEqual(len(mails), 2, f"the gap changed: {mails[-1]['text']}")
        self.assertNotEqual(first["text"].splitlines()[1:], mails[-1]["text"].splitlines()[1:])

    def test_an_items_age_alone_never_resends(self) -> None:
        self.started("slow", files="slow.py")
        journal = self.wt("slow") / ".campaign/demo/journal/slow.md"
        os.utime(journal, (time.time() - 7200,) * 2)
        self.look(0)
        (first,) = self.todo_mail()
        self.assertIn("todo: resume slow (nothing moved it for 2 h", first["text"])
        os.utime(journal, (time.time() - 3 * 3600,) * 2)
        for at in (300, 600, 900):
            self.look(at)
        self.assertEqual(len(self.todo_mail()), 1, "only its age changed")

    # A7: a line carries its age and evidence, and what the lead must do

    def test_a_line_carries_its_age_and_evidence(self) -> None:
        self.new("top")
        self.look(0)
        self.assertIn(
            "todo: start top (launchable; new): `campaign start top`, or park it with a reason",
            self.todo_mail()[-1]["text"],
        )
        self.look(1800)
        self.assertIn("todo: start top (launchable; listed 30 min)", self.todo_mail()[-1]["text"])

    def test_a_resume_line_names_the_jobs_and_an_answer_line_the_question(self) -> None:
        self.started("slow", files="slow.py")
        self.started("asks", files="asks.py")
        jid = self.submit(self.wt("slow"), "--quest", "slow", "--", "true")
        self.ok(self.home, "wait", jid, "--timeout", "60")
        self.journal("slow", f"waiting on jobs {jid}", ago=1200)  # its contributor stopped before they ended
        self.journal("asks", "blocked on the lead: which wire format", "Pick JSON or CBOR for the wire.")
        self.look(600)  # after the worker that ran the job last looked
        text = self.todo_mail()[-1]["text"]
        self.assertRegex(
            text,
            rf"todo: resume slow \(its journal waits on jobs {jid}, which passed \d+ min ago; new\): "
            r"`campaign status slow`, read them, or park it with a reason",
        )
        self.assertIn("it asks: Pick JSON or CBOR for the wire.", text)
        self.assertIn("answer it (`campaign message asks TEXT`), or park it with a reason", text)

    # A8: undone 2 h after its first send, the owner is told once and the lead's sidebar shows it

    def test_an_item_undone_two_hours_escalates_once_and_its_pill_clears(self) -> None:
        self.new("top")
        for at in (0, 1800, 3600, 5400):
            self.look(at)
        self.assertEqual(self.cmux("notify"), [])
        self.assertEqual(self.cmux("set-status"), [])
        self.look(7200)
        (told,) = self.cmux("notify")
        self.assertEqual(told[0], "ws-lead", "in the lead's workspace, which the worker never ran in")
        self.assertIn("demo: 1 item untouched 2 h+: start top", told)
        (pill,) = self.cmux("set-status")
        self.assertEqual(pill, ["ws-lead", "set-status", "campaign-demo--todo", "🚨 1 to do, oldest 2 h"])
        self.look(9000)
        self.assertEqual(len(self.cmux("notify")), 1, "no repeat before a new escalation")
        self.new("b")
        self.look(9300)
        self.look(9300 + 7200)
        self.assertEqual(len(self.cmux("notify")), 2, "a new item crossing 2 h is a new escalation")
        body = self.cmux("notify")[-1][-1]
        self.assertRegex(body, r"^demo: 2 items untouched 2 h\+: start (b, start top|top, start b)$")
        self.assertEqual(self.cmux("clear-status"), [])
        self.ok(self.home, "start", "top")
        self.ok(self.home, "start", "b")
        self.journal("top", "working")
        self.journal("b", "working")
        self.look(9300 + 7200 + 300)
        self.assertEqual(self.cmux("clear-status"), [["ws-lead", "clear-status", "campaign-demo--todo"]])
        self.assertEqual(len(self.cmux("notify")), 2)

    # A9: a reader that drops list lines keeps the to-do

    def test_every_todo_line_starts_with_todo(self) -> None:
        self.new("a", "b")
        for command in ("status", "brief"):
            out = self.ok(self.home, command).out
            lines = out.splitlines()
            head = next(i for i, x in enumerate(lines) if x.startswith("to do, critical path first"))
            items = [x for x in lines[head + 1 :] if "`campaign " in x]
            self.assertEqual(len(items), 2, out)
            self.assertTrue(all(x.startswith("todo: ") for x in items), out)
            kept = [x for x in lines if not x.startswith("- ")]
            self.assertTrue(all(x in kept for x in items))
