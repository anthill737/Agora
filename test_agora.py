"""Tests for the runner: every error class, the two timeouts, preflight, and the promise that Codex's auth file is
never written. Run with:  python -m unittest test_agora -v

A stub CLI stands in for the real ones. The model name chooses what it does, so the same launcher, command builder,
and reader that drive a real turn are exercised with nothing faked inside Agora itself."""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

import agora

PY = sys.executable
STUB_SRC = r'''
import json, os, sys, time
a = sys.argv[1:]
model = a[a.index("--model") + 1]
stream = "--stream" in a
d = os.environ.get("STUB_DIR")
calls = 0
if d:
    f = os.path.join(d, "calls_" + model + ".txt")
    calls = (int(open(f).read()) if os.path.exists(f) else 0) + 1
    open(f, "w").write(str(calls))
def out(s): sys.stdout.write(s + "\n"); sys.stdout.flush()
def err(s): sys.stderr.write(s + "\n"); sys.stderr.flush()
if stream:
    out(json.dumps({"type": "system", "session_id": "sess-1", "model": model}))
    if model == "stream-ok":
        out(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "OK"}]}}))
        out(json.dumps({"type": "result", "is_error": False, "result": "OK"})); sys.exit(0)
    if model == "stream-error":
        out(json.dumps({"type": "result", "is_error": True, "result": "Invalid model name: stream-error (404 not found)"})); sys.exit(1)
    if model == "stream-flag-rc0":
        out(json.dumps({"type": "result", "is_error": True, "result": "API Error: 401 authentication failed"})); sys.exit(0)
    if model == "stream-no-result":
        out(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "half"}]}})); sys.exit(0)
    sys.exit(2)
if model == "ok": out("OK"); sys.exit(0)
if model == "auth401": err("ERROR: 401 Unauthorized: token expired, please run codex login"); sys.exit(1)
if model == "revoked": err("ERROR: the refresh token was revoked; sign in again"); sys.exit(1)
if model == "model404": err("ERROR: 404 model_not_found: The model `model404` does not exist or you do not have access to it."); sys.exit(1)
if model == "noaccess": err("stream error: You do not have access to model noaccess"); sys.exit(1)
if model == "rate429": err("ERROR: 429 Too Many Requests: rate limit reached, try again later"); sys.exit(1)
if model == "err503": err("ERROR: 503 Service Unavailable"); sys.exit(1)
if model == "nocreds": err("ERROR: 401 Unauthorized: missing bearer token in request"); sys.exit(1)
if model == "rate-then-ok":
    if calls < 2: err("ERROR: 429 Too Many Requests"); sys.exit(1)
    out("OK"); sys.exit(0)
if model == "boom-then-ok":
    if calls < 2: err("ERROR: stream disconnected before completion"); sys.exit(1)
    out("OK"); sys.exit(0)
if model == "chatty-fail": out("I am a lovely message that must never become dialogue"); err("ERROR: stream disconnected before completion"); sys.exit(1)
if model == "empty": sys.exit(0)
if model == "hang": time.sleep(600); sys.exit(0)
if model == "drip":
    for i in range(3000): out("still thinking " + str(i)); time.sleep(0.3)
    sys.exit(0)
err("stub: unknown model " + model); sys.exit(3)
'''


class FakeAgora:
    def __init__(self) -> None:
        self.start_error = ""; self.away_url = ""; self.phone_url = ""


class Base(unittest.TestCase):
    """A temp home for sessions and the room, the stub registered as two providers, no Telegram, no real CLIs."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="agora-test-"))
        cls.stub = cls.tmp / "stub_cli.py"; cls.stub.write_text(STUB_SRC, encoding="utf-8")
        cmd = f'"{PY}" "{cls.stub}" --model {{model}} "{{ask}}"'
        agora.PROVIDERS["Stub"] = {"exe": PY, "speech": "stdout", "pkg": "", "cmd": cmd, "ro_cmd": cmd, "resume": "", "models": ["model404", "ok"]}
        agora.PROVIDERS["StubStream"] = {"exe": PY, "speech": "claude_stream", "pkg": "", "gate": True, "cmd": cmd + " --stream",
                                         "ro_cmd": cmd + " --stream", "resume": "", "models": ["stream-ok"]}
        for k in ("Stub", "StubStream"): agora._GATES[k] = threading.Lock()
        cls.saved = {k: getattr(agora, k) for k in ("SESSIONS", "ROOM", "TURN_TIMEOUT", "IDLE_TIMEOUT", "RATE_BACKOFF", "tg_config", "CODEX_AUTH", "CODEX_PROVIDERS", "HERE")}
        agora.SESSIONS = cls.tmp / "sessions"; agora.ROOM = cls.tmp / "room"; agora.SESSIONS.mkdir()
        agora.tg_config = lambda: {}
        agora.RATE_BACKOFF = (0.05, 0.05, 0.05)
        os.environ["STUB_DIR"] = str(cls.tmp)

    @classmethod
    def tearDownClass(cls) -> None:
        for k, v in cls.saved.items(): setattr(agora, k, v)
        for k in ("Stub", "StubStream"): agora.PROVIDERS.pop(k, None); agora._GATES.pop(k, None)
        os.environ.pop("STUB_DIR", None)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self) -> None:
        agora.MODELS_OK.clear(); agora.CONN.clear(); agora._LAST_PRIME.clear()
        for f in self.tmp.glob("calls_*.txt"): f.unlink()

    def calls(self, model: str) -> int:
        f = self.tmp / f"calls_{model}.txt"
        return int(f.read_text()) if f.exists() else 0

    def run_for(self, models: list[str], provider: str = "Stub", rounds: int = 1) -> tuple[agora.Run, FakeAgora]:
        s = agora.Session(agora.now_id())
        s.seats = agora.color_seats([{"name": f"Seat{i + 1}", "provider": provider, "model": m, "stance": ""} for i, m in enumerate(models)])
        s.skipped = [False] * len(s.seats); s.rounds = rounds; s.mode = "turns"; s.room = True; s.readonly = True
        fake = FakeAgora(); r = agora.Run(s, fake)
        return r, fake

    def speak(self, model: str, provider: str = "Stub") -> tuple[str | None, agora.Run]:
        r, _ = self.run_for([model, "ok"], provider)
        r.s.status = "running"
        text = r._speak(0, r.s.seats[0], "Say OK.")
        return text, r

    @staticmethod
    def notes(r: agora.Run) -> list[str]:
        return [e["text"] for e in r.s.transcript if e["kind"] == "system"]

    @staticmethod
    def speeches(r: agora.Run) -> list[dict]:
        return [e for e in r.s.transcript if e["kind"] in ("speech", "resolution")]

    @staticmethod
    def wait_for(cond, secs: float = 30) -> bool:
        end = time.time() + secs
        while time.time() < end:
            if cond(): return True
            time.sleep(0.1)
        return False


class Classify(unittest.TestCase):
    def test_classes(self) -> None:
        c = agora.classify
        self.assertEqual(c("401 Unauthorized"), "auth")
        self.assertEqual(c("the refresh token was revoked"), "auth")
        self.assertEqual(c("Not logged in. Run codex login"), "auth")
        self.assertEqual(c("404 model_not_found"), "model")
        self.assertEqual(c("The model `x` does not exist or you do not have access to it"), "model")
        self.assertEqual(c("429 Too Many Requests"), "rate")
        self.assertEqual(c("502 Bad Gateway"), "rate")
        self.assertEqual(c("stream error: overloaded"), "rate")
        self.assertEqual(c("401 Unauthorized: missing bearer token"), "launcher")
        self.assertEqual(c("no credentials found in the request"), "launcher")
        self.assertEqual(c("stream disconnected"), "other")
        self.assertEqual(c("gpt-5.6-luna answered in 5.01 s"), "other")   # version-like numbers are not status codes


class Launcher(Base):
    """run_turn reads the exit code and the error flag first; stdout is never speech when they say it failed."""

    def turn(self, model: str, provider: str = "Stub", **kw) -> agora.Turn:
        cmd = agora.build_cmd(provider, "Say OK.", model, True, str(self.tmp))
        return agora.run_turn(provider, cmd, str(self.tmp), **kw)

    def test_ok(self) -> None:
        t = self.turn("ok"); self.assertTrue(t.ok); self.assertEqual(t.speech, "OK"); self.assertEqual(t.rc, 0)

    def test_failed_stdout_is_never_speech(self) -> None:
        t = self.turn("chatty-fail")
        self.assertFalse(t.ok); self.assertIsNone(t.speech)
        self.assertIn("stream disconnected", t.error); self.assertEqual(t.kind, "other")
        self.assertIn("lovely message", "\n".join(t.stdout))   # it was printed, and it was not taken as the answer

    def test_exit_zero_but_nothing_printed_fails(self) -> None:
        t = self.turn("empty"); self.assertFalse(t.ok); self.assertIn("printed nothing", t.error)

    def test_stream_error_flag_beats_stdout(self) -> None:
        t = self.turn("stream-error", "StubStream")
        self.assertFalse(t.ok); self.assertEqual(t.kind, "model"); self.assertIn("Invalid model name", t.error); self.assertEqual(t.session_id, "sess-1")

    def test_stream_error_flag_beats_exit_code_zero(self) -> None:
        t = self.turn("stream-flag-rc0", "StubStream")
        self.assertFalse(t.ok); self.assertEqual(t.rc, 0); self.assertEqual(t.kind, "auth")

    def test_stream_without_final_result_fails(self) -> None:
        t = self.turn("stream-no-result", "StubStream"); self.assertFalse(t.ok); self.assertIn("no final answer", t.error)

    def test_stream_ok(self) -> None:
        t = self.turn("stream-ok", "StubStream"); self.assertTrue(t.ok); self.assertEqual(t.speech, "OK")

    def test_each_class(self) -> None:
        for model, kind in (("auth401", "auth"), ("revoked", "auth"), ("model404", "model"), ("noaccess", "model"),
                            ("rate429", "rate"), ("err503", "rate"), ("nocreds", "launcher")):
            t = self.turn(model); self.assertFalse(t.ok, model); self.assertEqual(t.kind, kind, model)

    def test_idle_timeout_kills_a_silent_process(self) -> None:
        t0 = time.time(); t = self.turn("hang", wall=60, idle=2)
        self.assertEqual(t.timed_out, "idle"); self.assertEqual(t.kind, "timeout"); self.assertFalse(t.ok)
        self.assertLess(time.time() - t0, 20); self.assertIsNotNone(t.rc)   # the process is gone, not orphaned
        self.assertIn("without printing", t.error)

    def test_wall_timeout_kills_a_process_that_keeps_printing(self) -> None:
        t0 = time.time(); t = self.turn("drip", wall=3, idle=60)
        self.assertEqual(t.timed_out, "wall"); self.assertFalse(t.ok); self.assertGreater(len(t.stdout), 3)
        self.assertLess(time.time() - t0, 20); self.assertIsNotNone(t.rc)

    def test_default_clocks_are_thirty_and_five_minutes(self) -> None:
        self.assertEqual(agora.TURN_TIMEOUT, 1800); self.assertEqual(agora.IDLE_TIMEOUT, 300)


class Turns(Base):
    """Run._speak: what each error class does to a conversation."""

    def test_success_is_speech(self) -> None:
        text, r = self.speak("ok"); self.assertEqual(text, "OK"); self.assertIn("ok", agora.MODELS_OK["Stub"])

    def test_failure_is_never_dialogue_and_is_asked_once_more(self) -> None:
        text, r = self.speak("chatty-fail")
        self.assertIsNone(text); self.assertEqual(self.calls("chatty-fail"), 2)
        self.assertEqual(self.speeches(r), [])
        self.assertTrue(any("sits this turn out" in n and "stream disconnected" in n for n in self.notes(r)))
        self.assertFalse(any("lovely message" in e["text"] for e in r.s.transcript))

    def test_other_error_then_ok_recovers(self) -> None:
        text, r = self.speak("boom-then-ok"); self.assertEqual(text, "OK"); self.assertEqual(self.calls("boom-then-ok"), 2)

    def test_model_error_switches_to_a_model_that_answered_the_probe(self) -> None:
        agora.note_model_ok("Stub", "ok")
        text, r = self.speak("model404")
        self.assertEqual(text, "OK"); self.assertEqual(r.s.seats[0]["model"], "ok")
        self.assertTrue(any("now uses ok" in n and "model404" in n for n in self.notes(r)))

    def test_model_error_with_nothing_probed_sits_out(self) -> None:
        text, r = self.speak("model404")
        self.assertIsNone(text); self.assertEqual(r.s.seats[0]["model"], "model404")
        self.assertTrue(any("sits this turn out" in n and "404" in n for n in self.notes(r)))

    def test_rate_limit_waits_then_succeeds(self) -> None:
        text, r = self.speak("rate-then-ok"); self.assertEqual(text, "OK"); self.assertEqual(self.calls("rate-then-ok"), 2)

    def test_rate_limit_exhausts_backoff_then_sits_out(self) -> None:
        text, r = self.speak("rate429")
        self.assertIsNone(text); self.assertEqual(self.calls("rate429"), 1 + len(agora.RATE_BACKOFF))
        self.assertTrue(any("sits this turn out" in n and "429" in n for n in self.notes(r)))

    def test_server_error_is_rate_class(self) -> None:
        text, r = self.speak("err503"); self.assertIsNone(text); self.assertEqual(self.calls("err503"), 1 + len(agora.RATE_BACKOFF))

    def test_auth_error_pauses_for_a_sign_in(self) -> None:
        text, r = self.speak("auth401")
        self.assertIsNone(text); self.assertEqual(r.s.status, "paused"); self.assertIn("signed out", r.notice); self.assertEqual(r.paused_for, "Stub")
        self.assertEqual(self.calls("auth401"), 2)   # asked once more after the sign-in was re-checked
        self.assertTrue(any("refused as signed out" in n and "401" in n for n in self.notes(r)))
        self.assertEqual(self.speeches(r), [])

    def test_missing_credentials_is_a_launcher_bug_that_stops_the_run(self) -> None:
        text, r = self.speak("nocreds")
        self.assertIsNone(text); self.assertTrue(r.stop_flag.is_set()); self.assertIn("no credentials", r.notice)
        self.assertEqual(self.calls("nocreds"), 1)   # never asked again
        cmd = agora.build_cmd("Stub", "x", "nocreds", True, "y").split("--model")[0]
        note = next(n for n in self.notes(r) if "launcher" in n)
        self.assertIn("The exact command that was built", note); self.assertIn(cmd.strip(), note); self.assertIn("--model nocreds", note)

    def test_timeout_is_asked_once_more_then_sits_out(self) -> None:
        agora.TURN_TIMEOUT, agora.IDLE_TIMEOUT = 60, 1
        try:
            text, r = self.speak("hang")
        finally:
            agora.TURN_TIMEOUT, agora.IDLE_TIMEOUT = self.saved["TURN_TIMEOUT"], self.saved["IDLE_TIMEOUT"]
        self.assertIsNone(text); self.assertEqual(self.calls("hang"), 2)
        self.assertTrue(any("sits this turn out" in n and "without printing" in n for n in self.notes(r)))


class CodexAuthFile(Base):
    """Agora never writes ~/.codex/auth.json, keeps no copy, and the old copy and its code are gone."""

    def test_old_backup_code_is_gone(self) -> None:
        for name in ("backup_codex_auth", "restore_codex_auth", "CODEX_BACKUP", "codex_recover", "codex_signed_out"):
            self.assertFalse(hasattr(agora, name), name)
        self.assertFalse(hasattr(agora.Run, "codex_recover"))

    def test_source_never_writes_the_auth_file(self) -> None:
        src = Path(agora.__file__).read_text(encoding="utf-8").splitlines()
        for ln in src:
            if ("CODEX_AUTH" in ln or "auth.json" in ln) and not ln.strip().startswith("#") and '"""' not in ln:
                for bad in ("write", "copy", "shutil", "unlink", "rename", "replace", "open("):
                    self.assertNotIn(bad, ln, ln)

    def test_leftover_backups_are_deleted_at_startup(self) -> None:
        agora.HERE = self.tmp / "agora.py"
        try:
            (self.tmp / "agora_codex_auth.bak").write_text("x"); (self.tmp / "agora_codex_auth.bak.old").write_text("x")
            gone = agora.forget_codex_backups()
            self.assertEqual(len(gone), 2); self.assertEqual(list(self.tmp.glob("agora_codex_auth.bak*")), [])
        finally:
            agora.HERE = self.saved["HERE"]

    def test_auth_flow_leaves_the_auth_file_untouched(self) -> None:
        auth = self.tmp / "auth.json"; body = json.dumps({"auth_mode": "chatgpt", "tokens": {"access_token": "t"}})
        auth.write_text(body, encoding="utf-8"); before = (auth.stat().st_mtime_ns, auth.read_text(encoding="utf-8"))
        agora.CODEX_AUTH = auth; agora.CODEX_PROVIDERS = ("Stub",)   # the stub plays Codex: its refusal goes through the Codex path
        try:
            self.assertEqual(agora.probe_codex_files()[0], "on")
            text, r = self.speak("auth401")
            self.assertIsNone(text); self.assertEqual(r.s.status, "paused")
            t = agora.codex_prime("Stub", "auth401", str(self.tmp), force=True); self.assertFalse(t.ok)
        finally:
            agora.CODEX_AUTH = self.saved["CODEX_AUTH"]; agora.CODEX_PROVIDERS = self.saved["CODEX_PROVIDERS"]
        self.assertEqual((auth.stat().st_mtime_ns, auth.read_text(encoding="utf-8")), before)
        self.assertEqual(list(Path(agora.__file__).parent.glob("agora_codex_auth.bak*")), [])


class Preflight(Base):
    def test_a_seat_that_fails_blocks_start(self) -> None:
        r, fake = self.run_for(["ok", "model404"])
        r.start()
        self.assertTrue(self.wait_for(lambda: not r.preflighting and r.s.status != "checking"))
        self.assertEqual(r.s.status, "idle"); self.assertIsNone(r.thread)
        self.assertIn("Not started", fake.start_error); self.assertIn("Seat2 (Stub, model404)", fake.start_error)
        self.assertIn("Pick another model", fake.start_error); self.assertIn("ok has answered", fake.start_error)
        self.assertEqual(self.speeches(r), [])

    def test_an_auth_failure_blocks_start_with_the_sign_in_hint(self) -> None:
        r, fake = self.run_for(["ok", "auth401"]); r.start()
        self.assertTrue(self.wait_for(lambda: not r.preflighting and r.s.status != "checking"))
        self.assertEqual(r.s.status, "idle"); self.assertIn("401", fake.start_error)

    def test_a_launcher_bug_blocks_start_with_the_exact_command(self) -> None:
        r, fake = self.run_for(["nocreds", "ok"]); r.start()
        self.assertTrue(self.wait_for(lambda: not r.preflighting and r.s.status != "checking"))
        self.assertIn("launcher bug", fake.start_error); self.assertIn("--model nocreds", fake.start_error)

    def test_probes_use_each_seats_own_model(self) -> None:
        r, fake = self.run_for(["ok", "boom-then-ok", "ok"]); r.start()
        self.assertTrue(self.wait_for(lambda: not r.preflighting and r.s.status != "checking"))
        self.assertEqual(self.calls("boom-then-ok"), 1); self.assertEqual(self.calls("ok"), 1)   # one probe per distinct model
        self.assertIn("boom-then-ok", fake.start_error)

    def test_everything_answers_then_the_conversation_runs(self) -> None:
        r, fake = self.run_for(["ok", "ok"]); r.start()
        self.assertTrue(self.wait_for(lambda: r.s.status in ("running", "done")))
        self.assertEqual(fake.start_error, "")
        self.assertTrue(self.wait_for(lambda: r.s.status == "done", 60))
        sp = self.speeches(r)
        self.assertEqual([e["kind"] for e in sp], ["speech", "speech", "resolution", "resolution"])
        self.assertTrue(all(e["text"] == "OK" for e in sp))


class Connections(Base):
    def test_connected_only_from_a_successful_probe(self) -> None:
        c = agora.check_conn("Stub")
        self.assertEqual(c["state"], "on"); self.assertEqual(c["model"], "ok")
        self.assertIn("ok answered a one-word request at", c["detail"]); self.assertTrue(c["version"])
        self.assertEqual(agora.default_model("Stub"), "ok"); self.assertEqual(agora.seat_model("Stub"), "ok")
        for p in ("Codex", "Codex (latest)"):
            self.assertEqual(agora.seat_model(p), "gpt-5.6-luna", "Codex seats default to luna")
            self.assertEqual(agora.PROVIDERS[p]["models"], ["gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.4-mini", "gpt-5.3-codex-spark"])
        self.assertEqual(agora.connections()["Stub"]["default_model"], "ok")

    def test_nothing_is_assumed_before_a_probe(self) -> None:
        self.assertEqual(agora.default_model("Stub"), "")
        self.assertEqual(agora.default_seats()[0]["provider"], "Claude Code")

    def test_a_refusal_is_not_connected(self) -> None:
        agora.PROVIDERS["Stub"]["models"] = ["auth401", "ok"]
        try:
            c = agora.check_conn("Stub"); self.assertEqual(c["state"], "off"); self.assertIn("401", c["detail"])
            self.assertEqual(self.calls("ok"), 0)   # a sign-in problem is not a model problem: no further models tried
        finally:
            agora.PROVIDERS["Stub"]["models"] = ["model404", "ok"]

    def test_two_codex_installs_warn(self) -> None:
        real = agora.cli_path; agora.cli_path = lambda p: "x"
        try:
            self.assertTrue(agora.shared_codex_warning("Codex")); self.assertTrue(agora.shared_codex_warning("Codex (latest)"))
            self.assertEqual(agora.shared_codex_warning("Claude Code"), [])
        finally:
            agora.cli_path = real


if __name__ == "__main__":
    unittest.main(verbosity=2)
