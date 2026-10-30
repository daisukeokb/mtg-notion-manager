"""Layer B (Phase 3H-R1) isolation self-check, run inside the test container.

Runs with plain Python before any test (never under pytest, so the repo-root
conftest.py / Layer A guard is NOT involved): every network probe below uses the
C-level `_socket` module, a child process, or a native binary (git/libcurl), so
it shows that the *container* blocks the traffic, independently of Layer A.

Order matters for safety:
1. Structural checks first (Linux only; network interfaces and routing tables).
   If any non-loopback interface or route exists, exit immediately WITHOUT
   sending a single probe packet.
2. Only when no external route can exist, run the probes (reserved
   documentation addresses only: 192.0.2.0/24, 2001:db8::/32, `.invalid`) and
   require an immediate "network unreachable"-style error (a timeout does not
   count as proof of isolation).
3. Secret / filesystem / privilege checks.

Exit code 0 only if every check passes; any failure exits non-zero so the test
suites never run in an environment that is not isolated (fail-closed).
"""

from __future__ import annotations

import errno
import os
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path

#: Dummy credentials the container must receive (never production values).
EXPECTED_DUMMY_ENV = {
    "NOTION_API_KEY": "test-dummy-notion-api-key-not-a-real-secret",
    "NOTION_COMMANDER_DATA_SOURCE_ID": "test-dummy-commander-data-source-id",
    "NOTION_CARD_DATA_SOURCE_ID": "test-dummy-card-data-source-id",
}

#: Environment variable names allowed to look secret-like (public, known values).
_ALLOWED_SECRETISH_NAMES = frozenset({"GPG_KEY", *EXPECTED_DUMMY_ENV})
_SECRETISH_MARKERS = ("TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL", "KEY", "AUTH")
_SECRETISH_PREFIXES = ("GITHUB_", "ACTIONS_", "RUNNER_", "AWS_", "AZURE_", "GOOGLE_", "GCP_")

#: errno values that prove the kernel has no route (immediate failure, no timeout).
UNREACHABLE_ERRNOS = frozenset(
    {errno.ENETUNREACH, errno.EHOSTUNREACH, errno.EADDRNOTAVAIL, errno.EAFNOSUPPORT}
)

IPV4_PROBE = ("192.0.2.1", 80)  # RFC 5737 TEST-NET-1
IPV6_PROBE = ("2001:db8::1", 80, 0, 0)  # RFC 3849 documentation prefix
INVALID_HOST = "isolation-check.invalid"  # RFC 6761


class IsolationCheckFailed(Exception):
    pass


# --- pure helpers (unit-tested under the normal pytest suite) -----------------


def external_interfaces(interface_names: Iterable[str]) -> list[str]:
    return sorted(name for name in interface_names if name != "lo")


def ipv4_routes(route_table_text: str) -> list[str]:
    """Non-header lines of /proc/net/route (any line is a route)."""
    lines = [line for line in route_table_text.splitlines() if line.strip()]
    return lines[1:]


def ipv6_non_loopback_routes(ipv6_route_text: str) -> list[str]:
    """Lines of /proc/net/ipv6_route whose device (last column) is not `lo`."""
    routes = []
    for line in ipv6_route_text.splitlines():
        fields = line.split()
        if fields and fields[-1] != "lo":
            routes.append(line)
    return routes


def secretish_env_names(environ: Mapping[str, str]) -> list[str]:
    found = []
    for name in environ:
        upper = name.upper()
        if name in _ALLOWED_SECRETISH_NAMES:
            continue
        if upper.startswith(_SECRETISH_PREFIXES) or any(m in upper for m in _SECRETISH_MARKERS):
            found.append(name)
    return sorted(found)


def dummy_env_mismatches(environ: Mapping[str, str]) -> list[str]:
    return sorted(
        name for name, value in EXPECTED_DUMMY_ENV.items() if environ.get(name) != value
    )


def dotenv_files(root: Path) -> list[str]:
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        if ".env" in filenames:
            found.append(str(Path(dirpath) / ".env"))
    return sorted(found)


def is_unreachable_error(exc: BaseException) -> bool:
    return isinstance(exc, OSError) and exc.errno in UNREACHABLE_ERRNOS


# --- checks ------------------------------------------------------------------


def _read(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def check_structure() -> None:
    if not sys.platform.startswith("linux"):
        raise IsolationCheckFailed(f"STRUCTURAL CHECK FAILED: not Linux ({sys.platform})")
    net_dir = Path("/sys/class/net")
    if not net_dir.is_dir():
        raise IsolationCheckFailed("STRUCTURAL CHECK FAILED: /sys/class/net not available")
    external = external_interfaces(p.name for p in net_dir.iterdir())
    if external:
        raise IsolationCheckFailed(
            f"STRUCTURAL CHECK FAILED: non-loopback network interfaces present: {external}"
        )
    routes4 = ipv4_routes(_read("/proc/net/route"))
    if routes4:
        raise IsolationCheckFailed(f"STRUCTURAL CHECK FAILED: IPv4 routes present: {routes4}")
    routes6 = ipv6_non_loopback_routes(_read("/proc/net/ipv6_route"))
    if routes6:
        raise IsolationCheckFailed(f"STRUCTURAL CHECK FAILED: IPv6 routes present: {routes6}")
    print("ok: only the loopback interface exists; no IPv4/IPv6 routes")


def _expect_unreachable(label: str, action: Callable[[], None]) -> None:
    import _socket

    try:
        action()
    except _socket.timeout as exc:  # ambiguous: does not prove isolation
        raise IsolationCheckFailed(f"PROBE FAILED: {label} timed out ({exc})") from exc
    except OSError as exc:
        if not is_unreachable_error(exc):
            raise IsolationCheckFailed(
                f"PROBE FAILED: {label} raised unexpected {exc!r}"
            ) from exc
        print(f"ok: {label} -> {errno.errorcode.get(exc.errno or 0, exc.errno)}")
        return
    raise IsolationCheckFailed(f"PROBE FAILED: {label} did not fail")


def check_probes() -> None:
    import _socket  # C-level module: bypasses any Python-level (Layer A) patching

    def tcp4() -> None:
        sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        sock.settimeout(5)
        try:
            sock.connect(IPV4_PROBE)
        finally:
            sock.close()

    def tcp6() -> None:
        sock = _socket.socket(_socket.AF_INET6, _socket.SOCK_STREAM)
        sock.settimeout(5)
        try:
            sock.connect(IPV6_PROBE)
        finally:
            sock.close()

    def udp4() -> None:
        sock = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        try:
            sock.sendto(b"layer-b-probe", (IPV4_PROBE[0], 53))
        finally:
            sock.close()

    _expect_unreachable("_socket TCP IPv4 connect 192.0.2.1:80", tcp4)
    _expect_unreachable("_socket TCP IPv6 connect [2001:db8::1]:80", tcp6)
    _expect_unreachable("_socket UDP IPv4 sendto 192.0.2.1:53", udp4)

    # DNS: no external nameserver may be reachable.
    for line in _read("/etc/resolv.conf").splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0] == "nameserver":
            server = fields[1]
            if server.startswith("127.") or server == "::1":
                continue  # loopback: cannot leave the container
            family = _socket.AF_INET6 if ":" in server else _socket.AF_INET

            def query(server: str = server, family: int = family) -> None:
                sock = _socket.socket(family, _socket.SOCK_DGRAM)
                try:
                    sock.sendto(b"\x00", (server, 53))
                finally:
                    sock.close()

            _expect_unreachable(f"_socket UDP sendto nameserver {server}:53", query)
    try:
        _socket.getaddrinfo(INVALID_HOST, 443)
    except _socket.gaierror as exc:
        print(f"ok: name resolution of {INVALID_HOST} failed ({exc})")
    else:
        raise IsolationCheckFailed(f"PROBE FAILED: {INVALID_HOST} resolved")

    # Child process (not affected by anything in this process).
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            "import _socket,sys\n"
            "s=_socket.socket(_socket.AF_INET,_socket.SOCK_STREAM);s.settimeout(5)\n"
            "try:\n s.connect(('192.0.2.1',80))\nexcept OSError as e:\n"
            " sys.stdout.write(str(e.errno));sys.exit(0)\nsys.exit(3)\n",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if child.returncode != 0 or int(child.stdout or -1) not in UNREACHABLE_ERRNOS:
        raise IsolationCheckFailed(
            f"PROBE FAILED: child process connect (rc={child.returncode}, out={child.stdout!r})"
        )
    print(f"ok: child process connect -> errno {child.stdout}")

    # Native (non-Python) networking: git over libcurl.
    native = subprocess.run(
        ["git", "ls-remote", "http://192.0.2.1/layer-b-probe.git"],
        capture_output=True,
        text=True,
        timeout=60,
        env={"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "/tmp"),
             "GIT_TERMINAL_PROMPT": "0"},
    )
    stderr = native.stderr.lower()
    if native.returncode == 0 or not (
        "unreachable" in stderr or "couldn't connect" in stderr or "failed to connect" in stderr
    ):
        raise IsolationCheckFailed(
            f"PROBE FAILED: native git/libcurl probe (rc={native.returncode}): "
            f"{native.stderr.strip()[:200]!r}"
        )
    print("ok: native git/libcurl connect failed immediately")


def check_secrets_and_privileges(app_root: Path) -> None:
    mismatches = dummy_env_mismatches(os.environ)
    if mismatches:
        raise IsolationCheckFailed(
            f"CREDENTIAL CHECK FAILED: not the expected dummy values: {mismatches}"
        )
    secretish = secretish_env_names(os.environ)
    if secretish:
        raise IsolationCheckFailed(
            f"CREDENTIAL CHECK FAILED: unexpected secret-like environment variables: {secretish}"
        )
    found = dotenv_files(app_root)
    if found:
        raise IsolationCheckFailed(f"CREDENTIAL CHECK FAILED: .env present: {found}")
    for socket_path in ("/var/run/docker.sock", "/run/docker.sock"):
        if os.path.exists(socket_path):
            raise IsolationCheckFailed(f"PRIVILEGE CHECK FAILED: {socket_path} is present")
    if os.getuid() == 0:
        raise IsolationCheckFailed("PRIVILEGE CHECK FAILED: running as root")
    probe = app_root / ".layer-b-write-probe"
    try:
        probe.write_text("x", encoding="utf-8")
    except OSError as exc:
        if exc.errno not in (errno.EROFS, errno.EACCES):
            raise IsolationCheckFailed(f"FILESYSTEM CHECK FAILED: {exc!r}") from exc
    else:
        probe.unlink()
        raise IsolationCheckFailed("FILESYSTEM CHECK FAILED: application directory is writable")
    print("ok: dummy credentials only, no secret-like env, no .env, no docker socket, "
          "non-root, read-only application directory")


def main(argv: list[str]) -> int:
    app_root = Path(argv[1]) if len(argv) > 1 else Path("/app")
    try:
        check_structure()  # must pass before any probe packet is sent
        check_probes()
        check_secrets_and_privileges(app_root)
    except IsolationCheckFailed as exc:
        print(f"ISOLATION CHECK FAILED: {exc}", file=sys.stderr)
        return 1
    print("LAYER B ISOLATION CHECK PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
