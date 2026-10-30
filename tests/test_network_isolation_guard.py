"""Phase 3H: テスト全体のネットワーク隔離・認証情報隔離ガード(リポジトリ直下の
conftest.py)自体の負のテスト。

安全性: すべてのテストは最初に `network_guard` fixture を要求する。ガードが無い環境
(修復前のコード)ではこのfixtureが存在しないためsetupで失敗し、テスト本体(接続の
試行)には到達しない。接続先は文書用に予約されたアドレス(192.0.2.0/24、2001:db8::/32)
と `.invalid`(RFC 6761)だけを使い、実在のサービスへは向けない。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unicodedata
import urllib.request
from pathlib import Path
from typing import Any

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

IPV4_TEST_NET = ("192.0.2.1", 80)  # RFC 5737 TEST-NET-1
IPV6_DOC = ("2001:db8::1", 80, 0, 0)  # RFC 3849 documentation prefix
INVALID_HOST = "network-guard-test.invalid"  # RFC 6761: never resolves
#: conftest.pyのTEST_DUMMY_ENV["NOTION_API_KEY"]と同じダミー値(子プロセスのpytestで使う)。
TEST_DUMMY_KEY = "test-dummy-notion-api-key-not-a-real-secret"


def _contains(exc: BaseException, error_type: type[BaseException]) -> bool:
    """例外chain・ExceptionGroupのどこかにガードの例外が含まれるか。"""
    seen: set[int] = set()
    stack: list[BaseException] = [exc]
    while stack:
        current = stack.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, error_type):
            return True
        stack.extend(getattr(current, "exceptions", ()) or ())
        for linked in (current.__cause__, current.__context__):
            if linked is not None:
                stack.append(linked)
    return False


def _ipv6_socket() -> socket.socket:
    try:
        return socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    except OSError as exc:  # IPv6未対応の環境
        pytest.skip(f"AF_INET6 socket not available: {exc}")


# --- 直接のsocket操作 ------------------------------------------------------------


def test_guard_is_installed_on_all_entry_points(network_guard: Any) -> None:
    for name in ("connect", "connect_ex", "sendto", "sendmsg", "bind"):
        if hasattr(socket.socket, name):
            assert getattr(getattr(socket.socket, name), "__network_guard__", False), name
    for name in (
        "create_connection",
        "getaddrinfo",
        "gethostbyname",
        "gethostbyname_ex",
        "gethostbyaddr",
        "getnameinfo",
    ):
        assert getattr(getattr(socket, name), "__network_guard__", False), name


@pytest.mark.parametrize("method", ["connect", "connect_ex"])
def test_ipv4_tcp_connect_blocked(network_guard: Any, method: str) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        with pytest.raises(network_guard.error_type):
            getattr(sock, method)(IPV4_TEST_NET)


@pytest.mark.parametrize("method", ["connect", "connect_ex"])
def test_ipv6_tcp_connect_blocked(network_guard: Any, method: str) -> None:
    with _ipv6_socket() as sock:
        with pytest.raises(network_guard.error_type):
            getattr(sock, method)(IPV6_DOC)


@pytest.mark.parametrize(
    ("family", "address"),
    [(socket.AF_INET, ("127.0.0.1", 9)), (socket.AF_INET6, ("::1", 9, 0, 0))],
    ids=["ipv4_loopback", "ipv6_loopback"],
)
def test_loopback_is_not_exempt(network_guard: Any, family: int, address: tuple) -> None:
    try:
        sock = socket.socket(family, socket.SOCK_STREAM)
    except OSError as exc:
        pytest.skip(f"socket family not available: {exc}")
    with sock, pytest.raises(network_guard.error_type):
        sock.connect(address)


def test_udp_sendto_blocked(network_guard: Any) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        with pytest.raises(network_guard.error_type):
            sock.sendto(b"x", IPV4_TEST_NET)


@pytest.mark.skipif(not hasattr(socket.socket, "sendmsg"), reason="no sendmsg on this platform")
def test_udp_sendmsg_blocked(network_guard: Any) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        with pytest.raises(network_guard.error_type):
            sock.sendmsg([b"x"], [], 0, IPV4_TEST_NET)


@pytest.mark.parametrize(
    ("family", "address"),
    [
        (socket.AF_INET, ("127.0.0.1", 0)),
        (socket.AF_INET6, ("::1", 0, 0, 0)),
        (socket.AF_INET, (INVALID_HOST, 0)),  # CPythonはbind内で名前解決するため
    ],
    ids=["ipv4", "ipv6", "ipv4_hostname"],
)
def test_inet_bind_blocked(network_guard: Any, family: int, address: tuple) -> None:
    try:
        sock = socket.socket(family, socket.SOCK_STREAM)
    except OSError as exc:
        pytest.skip(f"socket family not available: {exc}")
    with sock, pytest.raises(network_guard.error_type):
        sock.bind(address)


def test_create_connection_blocked(network_guard: Any) -> None:
    with pytest.raises(network_guard.error_type):
        socket.create_connection(IPV4_TEST_NET, timeout=1)


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("getaddrinfo", (INVALID_HOST, 443)),
        ("getaddrinfo", ("192.0.2.1", 443)),
        ("gethostbyname", (INVALID_HOST,)),
        ("gethostbyname_ex", (INVALID_HOST,)),
        ("gethostbyaddr", ("192.0.2.1",)),
        ("getnameinfo", (("192.0.2.1", 443), 0)),
    ],
)
def test_name_resolution_blocked(network_guard: Any, name: str, args: tuple) -> None:
    with pytest.raises(network_guard.error_type):
        getattr(socket, name)(*args)


def test_af_unix_still_allowed(network_guard: Any) -> None:
    """AF_UNIX(ファイルシステム上のsocket)は外部ネットワークではないため許可する
    (Generation WorkspaceのAF_UNIXテストが依存している)。"""
    if not hasattr(socket, "AF_UNIX"):
        pytest.skip("no AF_UNIX on this platform")
    short_dir = tempfile.mkdtemp(prefix="ng", dir="/tmp" if os.path.isdir("/tmp") else None)
    path = os.path.join(short_dir, "s")
    try:
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(path)
        except OSError as exc:
            server.close()
            pytest.skip(f"AF_UNIX bind not available here: {exc}")
        server.listen(1)
        with server, socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(path)
            peer, _ = server.accept()
            with peer:
                client.sendall(b"ok")
                assert peer.recv(2) == b"ok"
    finally:
        shutil.rmtree(short_dir, ignore_errors=True)


# --- HTTPクライアント・非同期 --------------------------------------------------


@pytest.mark.parametrize(
    "url", ["http://192.0.2.1/", f"https://{INVALID_HOST}/"], ids=["ip", "hostname"]
)
def test_httpx_sync_without_mock_blocked(network_guard: Any, url: str) -> None:
    """httpx_mock fixtureを要求しない(=pytest-httpxが差し替えない)テストでも、
    httpxの実通信はガードで止まる。"""
    with httpx.Client(timeout=2) as client, pytest.raises(BaseException) as info:
        client.get(url)
    assert _contains(info.value, network_guard.error_type), repr(info.value)


def test_httpx_async_without_mock_blocked(network_guard: Any) -> None:
    async def fetch() -> None:
        async with httpx.AsyncClient(timeout=2) as client:
            await client.get(f"https://{INVALID_HOST}/")

    with pytest.raises(BaseException) as info:
        asyncio.run(fetch())
    assert _contains(info.value, network_guard.error_type), repr(info.value)


def test_urllib_blocked(network_guard: Any) -> None:
    with pytest.raises(BaseException) as info:
        urllib.request.urlopen("http://192.0.2.1/", timeout=2)
    assert _contains(info.value, network_guard.error_type), repr(info.value)


def test_asyncio_open_connection_blocked(network_guard: Any) -> None:
    async def connect() -> None:
        await asyncio.open_connection("192.0.2.1", 80)

    with pytest.raises(BaseException) as info:
        asyncio.run(connect())
    assert _contains(info.value, network_guard.error_type), repr(info.value)


def test_block_message_does_not_echo_url_or_query(network_guard: Any) -> None:
    """socket層には URL・クエリ・ヘッダが渡らないため、遮断メッセージにも含まれない。"""
    with httpx.Client(timeout=2) as client, pytest.raises(BaseException) as info:
        client.get(
            "https://192.0.2.1/v1/pages?token=SECRET-QUERY",
            headers={"Authorization": "Bearer SECRET-HEADER"},
        )
    rendered = repr(info.value) + str(info.value)
    assert "SECRET-QUERY" not in rendered
    assert "SECRET-HEADER" not in rendered


# --- 認証情報の隔離 ----------------------------------------------------------------


def test_notion_env_vars_are_test_dummies(network_guard: Any) -> None:
    for name, value in network_guard.dummy_env.items():
        assert os.environ.get(name) == value


def test_real_config_load_uses_dummies_and_ignores_dotenv(network_guard: Any) -> None:
    """mockを付け忘れたテストが本物のConfig.load()を呼んでも、リポジトリ直下の .env は
    読まれず、ダミーの認証情報だけが使われる(.envの内容はこのテストでも読まない)。"""
    from mtg_notion_manager.config import Config

    config = Config.load()

    assert config.notion_api_key == network_guard.dummy_env["NOTION_API_KEY"]
    assert config.commander_data_source_id == network_guard.dummy_env[
        "NOTION_COMMANDER_DATA_SOURCE_ID"
    ]
    assert network_guard.credentials_isolated()


def test_load_dotenv_auto_discovery_is_disabled(network_guard: Any) -> None:
    import dotenv

    assert dotenv.load_dotenv() is False


def test_dotenv_values_auto_discovery_is_disabled(network_guard: Any) -> None:
    import dotenv

    assert dotenv.dotenv_values() == {}


def _path_variants(path: Path) -> list[str]:
    """同じファイルを指す、文字列としては異なるパスの表記(Unicode正規化・`..`・
    大文字小文字)。ファイルシステムによって同一ファイルになるかが異なる(例: APFSでは
    NFC/NFDや大文字小文字の違いが同じファイルを指すが、ext4では別のファイル名になる)
    ため、そのファイルシステム上で実際に同じファイルを指す表記だけを返す。"""
    text = str(path)
    candidates = {
        text,
        unicodedata.normalize("NFC", text),
        unicodedata.normalize("NFD", text),
        str(path.parent / "." / ".." / path.parent.name / path.name),
        str(path.with_name(path.name.upper())),
    }
    variants = []
    for candidate in sorted(candidates):
        try:
            if os.path.samefile(candidate, path):
                variants.append(candidate)
        except OSError:  # 別のファイル名として扱われ、存在しない
            continue
    assert text in variants
    return variants


def test_repo_dotenv_is_recognized_in_every_path_form(network_guard: Any) -> None:
    """本番の .env はstatだけで判定する(このテストでもファイルの内容は開かない)。"""
    repo_dotenv = REPO_ROOT / ".env"
    if not repo_dotenv.exists():
        pytest.skip("no repo-root .env in this environment (nothing to protect)")
    for variant in _path_variants(repo_dotenv):
        assert network_guard.is_protected_dotenv(variant), variant
    assert not network_guard.is_protected_dotenv(REPO_ROOT / "pyproject.toml")


def _dotenv_calls() -> dict[str, Any]:
    import dotenv
    import dotenv.main

    return {
        "load_dotenv_override": lambda p: dotenv.load_dotenv(dotenv_path=p, override=True),
        "dotenv_values": lambda p: dotenv.dotenv_values(p),
        "get_key": lambda p: dotenv.get_key(p, "PHASE3H_PROTECTED_VAR"),
        "DotEnv_dict": lambda p: dotenv.main.DotEnv(p).dict(),
        "set_key": lambda p: dotenv.set_key(p, "PHASE3H_PROTECTED_VAR", "changed"),
        "unset_key": lambda p: dotenv.unset_key(p, "PHASE3H_PROTECTED_VAR"),
    }


@pytest.mark.parametrize(
    "call",
    ["load_dotenv_override", "dotenv_values", "get_key", "DotEnv_dict", "set_key", "unset_key"],
)
def test_protected_dotenv_is_refused_for_every_api_and_path_form(
    network_guard: Any, tmp_path: Path, call: str
) -> None:
    """保護対象の .env は、python-dotenvのどのAPI・どのパス表記からも読み書きできない。
    本番の .env を使わず、非ASCIIのディレクトリ名を含む一時ファイル(ダミー内容)を
    テストの間だけ保護対象に加えて検証する。"""
    directory = tmp_path / unicodedata.normalize("NFC", "マイドライブ")
    directory.mkdir()
    protected = directory / ".env"
    protected.write_text("PHASE3H_PROTECTED_VAR=dummy-protected-value\n", encoding="utf-8")
    before_env = os.environ.get("PHASE3H_PROTECTED_VAR")

    with network_guard.additionally_protect(protected):
        for variant in _path_variants(protected):
            with pytest.raises(network_guard.credential_error_type):
                _dotenv_calls()[call](variant)

    assert os.environ.get("PHASE3H_PROTECTED_VAR") == before_env
    assert protected.read_text(encoding="utf-8") == (
        "PHASE3H_PROTECTED_VAR=dummy-protected-value\n"
    )
    assert network_guard.credentials_isolated()


def test_load_dotenv_with_explicit_path_still_works(
    network_guard: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """明示的に渡したテスト用ファイルの読み込みは従来通り(本番 .env ではない)。"""
    import dotenv

    monkeypatch.delenv("PHASE3H_SAMPLE_VAR", raising=False)
    env_file = tmp_path / "sample.env"
    env_file.write_text("PHASE3H_SAMPLE_VAR=from-test-file\n", encoding="utf-8")

    assert dotenv.load_dotenv(dotenv_path=env_file) is True
    assert os.environ["PHASE3H_SAMPLE_VAR"] == "from-test-file"
    monkeypatch.delenv("PHASE3H_SAMPLE_VAR")


def test_forgotten_mock_with_real_client_cannot_reach_notion(network_guard: Any) -> None:
    """mock・Config差し替えの両方を付け忘れた場合でも、ダミーの認証情報のまま
    名前解決の段階で遮断され、Notion APIへは到達しない。"""
    from mtg_notion_manager.config import Config
    from mtg_notion_manager.notion.client import NotionClient

    config = Config.load()
    assert config.notion_api_key == network_guard.dummy_env["NOTION_API_KEY"]
    with NotionClient(config.notion_api_key) as client, pytest.raises(BaseException) as info:
        client.get_current_user()
    assert _contains(info.value, network_guard.error_type), repr(info.value)


def test_subprocess_inherits_dummy_credentials(network_guard: Any) -> None:
    """子プロセスにはプロセス内のsocketガードは効かない(Layer Aの限界)が、
    環境変数はダミー値が引き継がれる。値そのものは出力せず一致判定だけ返させる。"""
    names = list(network_guard.dummy_env)
    code = (
        "import os,sys;"
        f"expected={network_guard.dummy_env!r};"
        "sys.stdout.write(str(all(os.environ.get(k)==v for k,v in expected.items())))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=30, check=True
    )
    assert names
    assert result.stdout == "True"


# --- 新しいpytestプロセスでの有効化タイミング -------------------------------------
#
# 子プロセスのpytestへリポジトリ直下のconftest.pyをそのまま置き、collection時・
# fixture setup/teardown時の遮断を確かめる。ガードが無くても通信が発生しないよう、
# 数値指定(AI_NUMERICHOST)のgetaddrinfoだけを使う。

_NUMERIC_LOOKUP = "socket.getaddrinfo('192.0.2.1', 80, flags=socket.AI_NUMERICHOST)"


def _run_inner_pytest(pytester: Any, test_source: str) -> Any:
    shutil.copy(REPO_ROOT / "conftest.py", pytester.path / "conftest.py")
    pytester.makepyfile(test_inner=test_source)
    return pytester.runpytest_subprocess("-q", "-p", "no:cacheprovider")


def test_guard_active_during_collection(network_guard: Any, pytester: Any) -> None:
    result = _run_inner_pytest(
        pytester,
        f"import socket\n{_NUMERIC_LOOKUP}\n\ndef test_never_runs():\n    pass\n",
    )
    result.stdout.fnmatch_lines(["*NetworkAccessBlockedError*"])
    assert result.ret != 0
    result.assert_outcomes(errors=1)


def test_guard_active_in_fixture_setup_and_teardown(network_guard: Any, pytester: Any) -> None:
    source = (
        "import socket\n"
        "import pytest\n\n"
        "@pytest.fixture\n"
        "def setup_lookup():\n"
        f"    {_NUMERIC_LOOKUP}\n"
        "    yield\n\n"
        "@pytest.fixture\n"
        "def teardown_lookup():\n"
        "    yield\n"
        f"    {_NUMERIC_LOOKUP}\n\n"
        "def test_setup(setup_lookup):\n"
        "    pass\n\n"
        "def test_teardown(teardown_lookup):\n"
        "    pass\n"
    )
    result = _run_inner_pytest(pytester, source)
    result.stdout.fnmatch_lines(["*NetworkAccessBlockedError*"])
    result.assert_outcomes(passed=1, errors=2)


def test_removing_guard_is_detected_and_reinstalled(network_guard: Any, pytester: Any) -> None:
    """テストがガードを外した(元に戻し忘れた)場合、autouseの確認でそのテストを
    エラーにし、ガードを再インストールして後続テストを保護する。"""
    source = (
        "import socket\n\n"
        "def test_a_removes_guard():\n"
        "    socket.getaddrinfo = lambda *a, **k: []\n"
        "    socket.socket.connect = lambda self, address: None\n\n"
        "def test_b_is_still_guarded():\n"
        "    assert getattr(socket.getaddrinfo, '__network_guard__', False)\n"
        "    assert getattr(socket.socket.connect, '__network_guard__', False)\n"
    )
    result = _run_inner_pytest(pytester, source)
    result.stdout.fnmatch_lines(["*this test removed the network guard*reinstalled*"])
    result.assert_outcomes(passed=2, errors=1)


def test_undoing_credential_isolation_is_detected_and_reinstalled(
    network_guard: Any, pytester: Any
) -> None:
    source = (
        "import os\n\n"
        "def test_a_changes_env():\n"
        "    os.environ['NOTION_API_KEY'] = 'not-the-dummy'\n\n"
        "def test_b_has_dummy_again():\n"
        f"    assert os.environ['NOTION_API_KEY'] == {TEST_DUMMY_KEY!r}\n"
    )
    result = _run_inner_pytest(pytester, source)
    result.stdout.fnmatch_lines(["*this test undid credential isolation*reinstalled*"])
    result.assert_outcomes(passed=2, errors=1)


def test_clearing_protected_dotenv_list_is_detected_and_reinstalled(
    network_guard: Any, pytester: Any
) -> None:
    """保護対象の .env リストからリポジトリ直下の .env を外した場合も検出し、
    再登録してからそのテストをエラーにする。"""
    source = (
        "import conftest\n\n"
        "def test_a_clears_list():\n"
        "    conftest._PROTECTED_DOTENV_FILES.clear()\n\n"
        "def test_b_list_restored():\n"
        "    assert conftest._REPO_DOTENV in conftest._PROTECTED_DOTENV_FILES\n"
    )
    result = _run_inner_pytest(pytester, source)
    result.stdout.fnmatch_lines(["*this test undid credential isolation*reinstalled*"])
    result.assert_outcomes(passed=2, errors=1)
