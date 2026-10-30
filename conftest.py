"""テスト全体のネットワーク隔離・認証情報隔離ガード(Phase 3H)。

このconftest.pyはリポジトリ直下(pytestのrootdir)に置かれているため、リポジトリ内の
テストを実行する通常のpytest(`pytest`、`pytest tools/generation_workspace/tests` 等)
では「初期conftest」として、テストのcollection・テスト対象モジュールのimportより前に
読み込まれる。ガードはこのモジュールのimport時点で有効になる。

Layer A(このファイル、pytestプロセス内)で保証すること:
- AF_INET / AF_INET6 のsocketに対する connect / connect_ex / sendto / sendmsg / bind、
  socket.create_connection、名前解決(getaddrinfo / gethostbyname /
  gethostbyname_ex / gethostbyaddr / getnameinfo)を、接続先に関わらず
  (localhost・::1 を含めて例外なく)NetworkAccessBlockedErrorで拒否する。
  httpx(同期・非同期)・urllib・ssl・asyncioはいずれもこれらを経由する。
- AF_UNIX(ファイルシステム上のsocket)は外部ネットワーク通信ではないため許可する。
- 認証情報: NOTION_* 環境変数をテスト用のダミー値で上書きし、python-dotenvの
  自動探索(dotenv_path未指定の load_dotenv / dotenv_values)を無効化する。
  リポジトリ直下の .env は、明示pathで指定しても python-dotenv の
  load_dotenv / dotenv_values / get_key / DotEnv から読めず、set_key / unset_key から
  書き換えられない(ファイルを開く前に CredentialAccessBlockedError)。判定はパス文字列
  ではなくファイルの同一性(os.path.samefile)で行う。.env ファイルは変更しない。
- 各テストの前後でガードが外れていないか確認し、外れていれば再インストールしたうえで
  そのテストをエラーにする。

保証しないこと(限界):
- このconftestが読み込まれない実行(`--noconftest`、`--confcutdir` にリポジトリ直下より
  下位のディレクトリを指定、リポジトリ外のテストファイル)ではガードは無効。
- subprocess・multiprocessing(spawn)の子プロセス、ネイティブ拡張・ctypesが直接行う通信、
  `_socket` モジュールや `super(socket.socket, s).connect` のようにPythonレベルの
  socket.socketを経由しない呼び出しには効かない(子プロセスにはダミーの環境変数だけが
  引き継がれる)。
- python-dotenv以外の手段(open()等)でファイルとして .env を読むこと(open()したstreamを
  dotenv_values(stream=...) 等へ渡すことを含む)は防がない。
- 実行環境レベルのネットワーク制限(Layer B)は行っていない。Windowsでは
  socket.socketpair がAF_INETを使うため未検証。

テストからガードを無効化するためのオプション・設定は提供しない。実際の外部通信が
必要な検証は、通常のpytestスイートとは別に行うこと。
"""

from __future__ import annotations

import contextlib
import os
import socket
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

#: ガード自体の負のテスト(新しいpytestプロセスでのcollection時・fixture時の遮断)に使う。
pytest_plugins = ["pytester"]

#: テスト中に使う認証情報のダミー値(本番の値ではない)。
TEST_DUMMY_ENV = {
    "NOTION_API_KEY": "test-dummy-notion-api-key-not-a-real-secret",
    "NOTION_COMMANDER_DATA_SOURCE_ID": "test-dummy-commander-data-source-id",
    "NOTION_CARD_DATA_SOURCE_ID": "test-dummy-card-data-source-id",
}


#: import時点の本物のpython-dotenv関数(再インストール時に使う)。
_ORIGINAL_DOTENV: dict[str, Callable[..., Any]] = {}


class NetworkAccessBlockedError(RuntimeError):
    """テスト中に外部ネットワーク通信(名前解決を含む)が試みられた。

    OSErrorのsubclassにしない: httpx/httpcore等はOSErrorを接続エラーへ変換して
    握りつぶしうるため、ガードによる遮断であることを呼び出し元まで伝播させる。
    """


_ALLOWED_FAMILIES = frozenset({getattr(socket, "AF_UNIX", None)} - {None})


def _family_name(family: Any) -> str:
    try:
        return socket.AddressFamily(family).name
    except (ValueError, TypeError):
        return str(family)


def _blocked(operation: str, detail: str = "") -> NetworkAccessBlockedError:
    # 接続先はホスト・ポート程度に留め、URL・ヘッダ・認証情報は扱わない(socket層には
    # そもそも渡ってこない)。
    suffix = f" ({detail})" if detail else ""
    return NetworkAccessBlockedError(
        f"テスト中のネットワーク通信は禁止されています: {operation}{suffix}。"
        "HTTPはpytest-httpx(httpx_mock)やfakeで差し替えてください。"
    )


def _describe_address(address: Any) -> str:
    if isinstance(address, tuple) and len(address) >= 2:
        return f"{address[0]}:{address[1]}"
    return type(address).__name__


def _guard_socket_method(name: str, original: Callable[..., Any]) -> Callable[..., Any]:
    def guarded(self: socket.socket, *args: Any, **kwargs: Any) -> Any:
        if self.family in _ALLOWED_FAMILIES:
            return original(self, *args, **kwargs)
        address = args[-1] if args else kwargs.get("address")
        raise _blocked(
            f"socket.{name}", f"{_family_name(self.family)} {_describe_address(address)}"
        )

    guarded.__name__ = name
    guarded.__dict__["__network_guard__"] = True
    return guarded


def _guard_function(name: str) -> Callable[..., Any]:
    def guarded(*args: Any, **kwargs: Any) -> Any:
        target = _describe_address(args[0]) if args and isinstance(args[0], tuple) else ""
        if not target and args:
            target = str(args[0])
        raise _blocked(f"socket.{name}", target)

    guarded.__name__ = name
    guarded.__dict__["__network_guard__"] = True
    return guarded


_GUARDED_SOCKET_METHODS = ("connect", "connect_ex", "sendto", "sendmsg", "bind")
_GUARDED_FUNCTIONS = (
    "create_connection",
    "getaddrinfo",
    "gethostbyname",
    "gethostbyname_ex",
    "gethostbyaddr",
    "getnameinfo",
)


#: import時点の本物のsocketメソッド。再インストール時に、テストが差し替えた関数を
#: 「本物」として包まないようにするため最初の1回だけ記録する。
_ORIGINAL_SOCKET_METHODS: dict[str, Callable[..., Any]] = {
    name: getattr(socket.socket, name)
    for name in _GUARDED_SOCKET_METHODS
    if hasattr(socket.socket, name)
    and not getattr(getattr(socket.socket, name), "__network_guard__", False)
}


def _install_network_guard() -> None:
    for name, original in _ORIGINAL_SOCKET_METHODS.items():
        if getattr(getattr(socket.socket, name, None), "__network_guard__", False):
            continue
        setattr(socket.socket, name, _guard_socket_method(name, original))
    for name in _GUARDED_FUNCTIONS:
        current = getattr(socket, name, None)
        if current is None or getattr(current, "__network_guard__", False):
            continue
        setattr(socket, name, _guard_function(name))


def _network_guard_installed() -> bool:
    methods_ok = all(
        getattr(getattr(socket.socket, name, None), "__network_guard__", False)
        for name in _GUARDED_SOCKET_METHODS
        if hasattr(socket.socket, name)
    )
    functions_ok = all(
        getattr(getattr(socket, name, None), "__network_guard__", False)
        for name in _GUARDED_FUNCTIONS
        if hasattr(socket, name)
    )
    return methods_ok and functions_ok


#: テスト中に内容を読ませない .env ファイル(既定はリポジトリ直下の本番用 .env)。
#: 判定はパス文字列ではなくファイルの同一性(os.path.samefile)で行う。Unicode正規化
#: (NFC/NFD)・大文字小文字・`..`・シンボリックリンク・ハードリンクの違いに影響されない。
_REPO_DOTENV = str(Path(__file__).resolve().parent / ".env")
_PROTECTED_DOTENV_FILES: list[str] = [_REPO_DOTENV]


class CredentialAccessBlockedError(RuntimeError):
    """テスト中に保護対象の .env(リポジトリ直下の本番用 .env)を読み書きしようとした。"""


def _is_protected_dotenv(candidate: Any) -> bool:
    if candidate is None:
        return False
    try:
        path = os.fspath(candidate)
    except TypeError:
        return False
    for protected in _PROTECTED_DOTENV_FILES:
        try:
            if os.path.samefile(path, protected):
                return True
        except OSError:  # どちらかが存在しない: 同一ファイルではない
            continue
    return False


def _refuse_protected_dotenv(candidate: Any, operation: str) -> None:
    if _is_protected_dotenv(candidate):
        # 内容は読まない(ファイルの同一性だけで判定する)。
        raise CredentialAccessBlockedError(
            f"テスト中にリポジトリ直下の .env を読み書きすることは禁止されています: {operation}。"
            "テスト用の設定は一時ファイルやmonkeypatchで用意してください。"
        )


def _isolate_credentials() -> None:
    # 開発者のshellで本番の値がexportされていても、テストではダミー値だけを使う。
    for name, value in TEST_DUMMY_ENV.items():
        os.environ[name] = value
    if _REPO_DOTENV not in _PROTECTED_DOTENV_FILES:
        _PROTECTED_DOTENV_FILES.insert(0, _REPO_DOTENV)

    try:
        import dotenv
        import dotenv.main
    except ImportError:  # python-dotenvを使わない環境(Generation Workspace単体等)
        return

    original_load_dotenv = _ORIGINAL_DOTENV.setdefault("load_dotenv", dotenv.main.load_dotenv)
    original_dotenv_values = _ORIGINAL_DOTENV.setdefault(
        "dotenv_values", dotenv.main.dotenv_values
    )
    original_dotenv_init = _ORIGINAL_DOTENV.setdefault(
        "DotEnv.__init__", dotenv.main.DotEnv.__init__
    )
    original_rewrite = _ORIGINAL_DOTENV.setdefault("rewrite", dotenv.main.rewrite)

    def guarded_load_dotenv(dotenv_path: Any = None, stream: Any = None, **kwargs: Any) -> bool:
        # dotenv_path/streamを明示しない呼び出しは、呼び出し元から親ディレクトリを辿って
        # リポジトリ直下の本番用 .env を見つけてしまうため、テスト中は何も読まない。
        if dotenv_path is None and stream is None:
            return False
        return bool(original_load_dotenv(dotenv_path=dotenv_path, stream=stream, **kwargs))

    def guarded_dotenv_values(
        dotenv_path: Any = None, stream: Any = None, **kwargs: Any
    ) -> dict[str, Any]:
        if dotenv_path is None and stream is None:
            return {}
        return dict(original_dotenv_values(dotenv_path=dotenv_path, stream=stream, **kwargs))

    def guarded_dotenv_init(self: Any, dotenv_path: Any, *args: Any, **kwargs: Any) -> None:
        # load_dotenv / dotenv_values / get_key / DotEnv直接利用のいずれも、ファイルを
        # 開く前にここを通る(DotEnv._get_streamがdotenv_pathを開く)。
        _refuse_protected_dotenv(dotenv_path, "DotEnv")
        original_dotenv_init(self, dotenv_path, *args, **kwargs)

    def guarded_rewrite(path: Any, *args: Any, **kwargs: Any) -> Any:
        # set_key / unset_key の書き込み経路(touch・open・置き換えの前に拒否する)。
        _refuse_protected_dotenv(path, "rewrite")
        return original_rewrite(path, *args, **kwargs)

    for guarded in (
        guarded_load_dotenv,
        guarded_dotenv_values,
        guarded_dotenv_init,
        guarded_rewrite,
    ):
        guarded.__dict__["__credential_guard__"] = True
    for module in (dotenv, dotenv.main):
        module.__dict__["load_dotenv"] = guarded_load_dotenv
        module.__dict__["dotenv_values"] = guarded_dotenv_values
    dotenv.main.__dict__["rewrite"] = guarded_rewrite
    init_name = "__init__"
    setattr(dotenv.main.DotEnv, init_name, guarded_dotenv_init)

    try:
        import mtg_notion_manager.config as app_config
    except ImportError:
        return
    app_config.__dict__["load_dotenv"] = guarded_load_dotenv


def _credentials_isolated() -> bool:
    if any(os.environ.get(name) != value for name, value in TEST_DUMMY_ENV.items()):
        return False
    if _REPO_DOTENV not in _PROTECTED_DOTENV_FILES:
        return False
    try:
        import dotenv
        import dotenv.main
    except ImportError:
        return True
    guarded_objects = [
        getattr(module, name, None)
        for module in (dotenv, dotenv.main)
        for name in ("load_dotenv", "dotenv_values")
    ]
    guarded_objects += [dotenv.main.rewrite, dotenv.main.DotEnv.__init__]
    if not all(getattr(obj, "__credential_guard__", False) for obj in guarded_objects):
        return False
    try:
        import mtg_notion_manager.config as app_config
    except ImportError:
        return True
    return bool(getattr(app_config.load_dotenv, "__credential_guard__", False))


# import時点(collection前)に有効化する。
_install_network_guard()
_isolate_credentials()


class NetworkGuardInfo:
    """ガード自体を検証するテスト用の情報(network_guard fixture)。"""

    error_type = NetworkAccessBlockedError
    credential_error_type = CredentialAccessBlockedError
    dummy_env = dict(TEST_DUMMY_ENV)

    @staticmethod
    def installed() -> bool:
        return _network_guard_installed()

    @staticmethod
    def credentials_isolated() -> bool:
        return _credentials_isolated()

    @staticmethod
    def is_protected_dotenv(path: Any) -> bool:
        """ファイルを読まずに(statだけで)保護対象の .env かどうかを判定する。"""
        return _is_protected_dotenv(path)

    @staticmethod
    @contextlib.contextmanager
    def additionally_protect(path: Any) -> Iterator[None]:
        """テスト用の一時ファイルを、テストの間だけ保護対象に追加する(保護を外す手段は
        提供しない)。本番の .env を使わずに拒否の動作を検証するためのもの。"""
        entry = os.fspath(path)
        _PROTECTED_DOTENV_FILES.append(entry)
        try:
            yield
        finally:
            _PROTECTED_DOTENV_FILES.remove(entry)


@pytest.fixture
def network_guard() -> NetworkGuardInfo:
    assert _network_guard_installed(), "network guard is not installed"
    return NetworkGuardInfo()


@pytest.fixture(autouse=True)
def _network_and_credential_guard_intact() -> Iterator[None]:
    """各テストの前後で、ガードが外されていない・認証情報がダミーのままであることを
    確認する(テストの実行順序やmonkeypatchの戻し忘れでガードが無効化されたまま
    後続テストが走ることを防ぐ)。"""
    _ensure_guards(
        "before this test", "network guard was removed", "credential isolation was undone"
    )
    yield
    _ensure_guards(
        "by this test",
        "this test removed the network guard",
        "this test undid credential isolation",
    )


def _ensure_guards(when: str, network_message: str, credential_message: str) -> None:
    """ガードが外れていれば先に再インストールし(後続テストを保護する)、そのうえで
    このテストを失敗させる。"""
    network_ok = _network_guard_installed()
    credentials_ok = _credentials_isolated()
    if not network_ok:
        _install_network_guard()
    if not credentials_ok:
        _isolate_credentials()
    assert network_ok, f"{network_message} ({when}); reinstalled"
    assert credentials_ok, f"{credential_message} ({when}); reinstalled"
