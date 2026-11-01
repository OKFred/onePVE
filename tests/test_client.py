"""Windows tunnel acceptance with a local SSH command stub, never a remote host."""
import base64
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "client" / "AgentTunnel.ps1"
POWERSHELL = shutil.which("powershell.exe") if os.name == "nt" else None


def ps_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def run_powershell(source, env=None, timeout=25):
    encoded = base64.b64encode(source.encode("utf-16le")).decode("ascii")
    return subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-EncodedCommand", encoded], env=env, timeout=timeout, capture_output=True,
        encoding="utf-8", errors="replace",
    )


def unused_ports(count=2):
    sockets = [socket.socket() for _ in range(count)]
    try:
        for item in sockets:
            item.bind(("127.0.0.1", 0))
        return [item.getsockname()[1] for item in sockets]
    finally:
        for item in sockets:
            item.close()


@unittest.skipUnless(POWERSHELL, "Windows PowerShell is required for command-stub tests")
class ClientTunnelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tools = tempfile.TemporaryDirectory(prefix="onePVE ssh stub ")
        cls.stub = Path(cls.tools.name)
        source = cls.stub / "Stub.cs"
        source.write_text(r'''
using System;
using System.IO;
using System.Threading;
public static class SshStub {
    public static int Main(string[] args) {
        string directory = Environment.GetEnvironmentVariable("ONEPVE_SSH_STUB_DIR");
        if (Array.IndexOf(args, "-G") >= 0) {
            Console.WriteLine("hostname example.invalid\nuser agent\nport 22");
            if (Environment.GetEnvironmentVariable("ONEPVE_STUB_FORWARD") == "1")
                Console.WriteLine("localforward 0.0.0.0:9999 localhost:9999");
            return 0;
        }
        string pid = System.Diagnostics.Process.GetCurrentProcess().Id.ToString();
        File.WriteAllLines(Path.Combine(directory, "run-" + pid + ".txt"), args);
        string first = Path.Combine(directory, "first-exit");
        if (Environment.GetEnvironmentVariable("ONEPVE_STUB_RETRY") == "1" && !File.Exists(first)) {
            File.WriteAllText(first, pid);
            return 17;
        }
        Thread.Sleep(60000);
        return 0;
    }
}
''', encoding="utf-8")
        compiled = run_powershell(
            "$ErrorActionPreference='Stop'; Add-Type -TypeDefinition "
            f"(Get-Content -Raw -LiteralPath {ps_quote(source)}) "
            f"-OutputAssembly {ps_quote(cls.stub / 'ssh.exe')} -OutputType ConsoleApplication"
        )
        if compiled.returncode:
            raise AssertionError(compiled.stderr + compiled.stdout)

    @classmethod
    def tearDownClass(cls):
        cls.tools.cleanup()

    def setUp(self):
        self.work = tempfile.TemporaryDirectory(prefix="onePVE tunnel test ")
        self.directory = Path(self.work.name)
        self.config = self.directory / "ssh config"
        self.config.write_text("Host agent-test\n HostName example.invalid\n User agent\n", encoding="utf-8")
        self.viewer, self.mcp = unused_ports()
        self.env = dict(os.environ, PATH=str(self.stub) + os.pathsep + os.environ["PATH"],
                        ONEPVE_SSH_STUB_DIR=str(self.directory))

    def tearDown(self):
        try:
            self.call("Stop")
        finally:
            self.work.cleanup()

    def call(self, action, *, alias="agent-test", check=True, viewer=None, mcp=None):
        source = "$ErrorActionPreference='Stop'; [Console]::OutputEncoding=New-Object Text.UTF8Encoding($false); try { "
        source += f"& {ps_quote(HELPER)} -Action {action} -SshAlias {ps_quote(alias)} "
        source += f"-SshConfig {ps_quote(self.config)} -StateRoot {ps_quote(self.directory / 'state')} "
        source += f"-ViewerPort {viewer or self.viewer} -McpPort {mcp or self.mcp} | ConvertTo-Json -Depth 8"
        source += " } catch { [Console]::Error.WriteLine($_.Exception.Message); exit 1 }"
        result = run_powershell(source, self.env)
        if check:
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            return json.loads(result.stdout)
        return result

    def wait_for_attempts(self, count):
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            attempts = list(self.directory.glob("run-*.txt"))
            if len(attempts) >= count:
                return attempts
            time.sleep(0.1)
        self.fail(f"Expected {count} SSH attempts; got {attempts}")

    def test_reconnect_exact_arguments_and_cooperative_stop(self):
        self.env["ONEPVE_STUB_RETRY"] = "1"
        started = self.call("Start")
        self.assertTrue(started["Running"])
        attempts = self.wait_for_attempts(2)
        arguments = attempts[-1].read_text().splitlines()
        for required in ("BatchMode=yes", "StrictHostKeyChecking=yes", "IdentitiesOnly=yes",
                         "ExitOnForwardFailure=yes", "GatewayPorts=no", "ClearAllForwardings=no", "ControlPath=none",
                         "PermitLocalCommand=no", "ServerAliveInterval=30", "ServerAliveCountMax=3"):
            self.assertIn(required, arguments)
        self.assertIn(str(self.config), arguments)
        self.assertIn(f"127.0.0.1:{self.viewer}:127.0.0.1:6080", arguments)
        self.assertIn(f"127.0.0.1:{self.mcp}:127.0.0.1:8931", arguments)
        self.assertEqual(arguments[-1], "agent-test")
        self.assertTrue(self.call("Start")["Running"])
        self.assertEqual(len(list(self.directory.glob("run-*.txt"))), 2)
        self.assertFalse(self.call("Stop")["Running"])
        self.assertFalse(self.call("Status")["Running"])
        time.sleep(0.3)
        self.assertEqual(len(list(self.directory.glob("run-*.txt"))), 2)

    def test_stop_never_uses_pid_from_status_file(self):
        unrelated = subprocess.Popen(
            [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", "Start-Sleep 60"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            self.call("Start")
            self.wait_for_attempts(1)
            status = self.call("Status")
            state_file = Path(status["StateDirectory"]) / "status.json"
            state_file.write_text(json.dumps({"supervisor_pid": unrelated.pid, "ssh_pid": unrelated.pid}))
            self.assertFalse(self.call("Stop")["Running"])
            self.assertIsNone(unrelated.poll())
        finally:
            unrelated.terminate()
            unrelated.wait(timeout=5)

    def test_wrong_alias_stop_does_not_stop_our_tunnel(self):
        self.call("Start")
        self.wait_for_attempts(1)
        self.assertFalse(self.call("Stop", alias="different-alias")["Running"])
        self.assertTrue(self.call("Status")["Running"])

    def test_occupied_port_refuses_without_starting_ssh(self):
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", self.viewer))
            occupied.listen(1)
            result = self.call("Start", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("occupied", result.stderr)
        self.assertFalse(list(self.directory.glob("run-*.txt")))

    def test_alias_options_and_inherited_forwards_refused(self):
        self.assertNotEqual(self.call("Start", alias="-oProxyCommand=bad", check=False).returncode, 0)
        self.env["ONEPVE_STUB_FORWARD"] = "1"
        result = self.call("Start", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("without configured forwards", result.stderr)
        self.assertFalse(list(self.directory.glob("run-*.txt")))

    def test_equal_ports_refused(self):
        result = self.call("Start", mcp=self.viewer, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must differ", result.stderr)
        self.assertFalse(list(self.directory.glob("run-*.txt")))


if __name__ == "__main__":
    unittest.main()
