"""The Mac Keychain and Linux keyring backends, driven through a stand-in for their command-line tools."""

import shlex
import subprocess

import keystore


class FakeTool:
    """Answers like `security` (Mac) or `secret-tool` (Linux), keeping secrets in a dict."""

    def __init__(self):
        self.secrets = {}
        self.argvs = []

    def __call__(self, argv, input=None, **_):
        self.argvs.append(argv)
        tool, args = argv[0], argv[1:]
        ok = lambda out="": subprocess.CompletedProcess(argv, 0, out, "")  # noqa: E731
        if tool == "security":
            if args[0] == "-i":   # interactive: one command on stdin
                words = shlex.split(input)
                self.secrets[words[words.index("-s") + 1]] = words[words.index("-w") + 1]
                return ok()
            target = args[args.index("-s") + 1]
            if target not in self.secrets:
                return subprocess.CompletedProcess(argv, 44, "", "The specified item could not be found.")
            if args[0] == "find-generic-password":
                return ok(self.secrets[target] + "\n")
            del self.secrets[target]
            return ok()
        target = args[args.index("target") + 1]
        if args[0] == "store":
            self.secrets[target] = input
            return ok()
        if args[0] == "lookup":
            return ok(self.secrets[target]) if target in self.secrets else subprocess.CompletedProcess(argv, 1, "", "")
        self.secrets.pop(target, None)
        return ok()


def _round_trip(backend, monkeypatch):
    fake = FakeTool()
    monkeypatch.setattr(subprocess, "run", fake)
    secret = 'sk-ant-with "quotes" and \\ backslash'
    assert backend.get("CodeMonkey:provider:x") is None
    backend.set("CodeMonkey:provider:x", secret)
    assert backend.get("CodeMonkey:provider:x") == secret
    assert not any(secret in " ".join(argv) for argv in fake.argvs), "the key must not be on a command line"
    assert backend.delete("CodeMonkey:provider:x") is True
    assert backend.get("CodeMonkey:provider:x") is None
    assert backend.delete("CodeMonkey:provider:x") is False


def test_mac_keychain_keeps_keys_off_the_command_line(monkeypatch):
    backend = keystore._KeychainBackend()
    backend._exe = "security"
    _round_trip(backend, monkeypatch)


def test_linux_keyring_through_secret_tool(monkeypatch):
    _round_trip(keystore._SecretToolBackend("secret-tool"), monkeypatch)


def test_store_name_says_where_keys_go(monkeypatch):
    monkeypatch.setattr(keystore, "_backend", keystore._SecretToolBackend("secret-tool"))
    assert keystore.store_name() == "the system keyring"
    monkeypatch.setattr(keystore, "_backend", None)
    assert keystore.store_name() == ""
