"""Error Contract v2 optional mutation metadata.

This module models write-batch outcomes for future mutating commands
(``import-cards``, the dedupe family, ...). It is pure data + validation:
nothing here decides *when* a command should attach a :class:`MutationSummary`
to an ``error_contract.py`` response, or what its ``error_category``/
``error_code``/``operations[].action`` vocabulary should be — that is each
command's own integration work, not yet performed as of this module's
introduction.

``MutationSummary`` is deliberately immutable and self-validating: ``state``
is always *derived* from the attempted/succeeded/failed/unknown counts (a
caller cannot construct a self-contradictory state), and ``recovery_action``
is validated against what that derived state allows — in particular,
``MUTATION_STATE_UNKNOWN`` can never be paired with a recovery action that
would permit a blind retry.
"""

from __future__ import annotations

from dataclasses import dataclass, field


class MutationState:
    """The five mutation-state values a :class:`MutationSummary` can derive to."""

    NO_MUTATION = "NO_MUTATION"
    MUTATION_SUCCEEDED = "MUTATION_SUCCEEDED"
    PARTIAL_MUTATION = "PARTIAL_MUTATION"
    MUTATION_FAILED = "MUTATION_FAILED"
    MUTATION_STATE_UNKNOWN = "MUTATION_STATE_UNKNOWN"


class RecoveryAction:
    """What a machine consumer may safely do next. Never auto-inferred from
    the mutation state alone beyond what :data:`_ALLOWED_RECOVERY_BY_STATE`
    permits — a command's own contract must justify ``RETRY_ALLOWED``.
    """

    NONE = "NONE"
    RETRY_ALLOWED = "RETRY_ALLOWED"
    RECONCILE_BEFORE_RETRY = "RECONCILE_BEFORE_RETRY"
    MANUAL_REVIEW_REQUIRED = "MANUAL_REVIEW_REQUIRED"


_RECOVERY_ACTIONS = frozenset(
    {
        RecoveryAction.NONE,
        RecoveryAction.RETRY_ALLOWED,
        RecoveryAction.RECONCILE_BEFORE_RETRY,
        RecoveryAction.MANUAL_REVIEW_REQUIRED,
    }
)

# state -> recovery_action値のうち、その状態で意味的に許容できる集合。
# MUTATION_STATE_UNKNOWN(completion状態が不明な書き込みが1件以上ある)から
# RETRY_ALLOWEDへ到達できないことが、このモジュール全体で最も重要な制約。
_ALLOWED_RECOVERY_BY_STATE: dict[str, frozenset[str]] = {
    MutationState.NO_MUTATION: frozenset({RecoveryAction.NONE}),
    MutationState.MUTATION_SUCCEEDED: frozenset({RecoveryAction.NONE}),
    MutationState.PARTIAL_MUTATION: frozenset(
        {RecoveryAction.RECONCILE_BEFORE_RETRY, RecoveryAction.MANUAL_REVIEW_REQUIRED}
    ),
    MutationState.MUTATION_FAILED: frozenset(
        {
            RecoveryAction.RETRY_ALLOWED,
            RecoveryAction.RECONCILE_BEFORE_RETRY,
            RecoveryAction.MANUAL_REVIEW_REQUIRED,
        }
    ),
    MutationState.MUTATION_STATE_UNKNOWN: frozenset(
        {RecoveryAction.RECONCILE_BEFORE_RETRY, RecoveryAction.MANUAL_REVIEW_REQUIRED}
    ),
}

# operation detailは failed/unknown の補助情報のみ(成功operationは含めない)。
_OPERATION_STATES = frozenset({"failed", "unknown"})


@dataclass(frozen=True)
class MutationOperation:
    """failed/unknown となった1件のwrite操作についての補助detail。

    ``key`` はcallerが既に知っているnon-sensitiveな識別子(例: カード表示名)を
    想定する。このinfrastructure層はNotion page_id/page_url/APIトークン等の
    生成・要求を一切行わない — それらをmachine outputへ含めるかどうかは
    各command側の判断であり、含めない方向を既定として推奨する。
    """

    key: str
    action: str
    state: str

    def __post_init__(self) -> None:
        if not self.key:
            raise ValueError("MutationOperation.key must be a non-empty string.")
        if not self.action:
            raise ValueError("MutationOperation.action must be a non-empty string.")
        if self.state not in _OPERATION_STATES:
            raise ValueError(
                f"MutationOperation.state must be one of {sorted(_OPERATION_STATES)}, "
                f"got {self.state!r}."
            )

    def to_dict(self) -> dict:
        return {"key": self.key, "action": self.action, "state": self.state}


def _derive_state(attempted: int, succeeded: int, failed: int, unknown: int) -> str:
    """attempted/succeeded/failed/unknownから決定論的にstateを導出する。

    callerがstateを直接指定できるAPIにしない(矛盾したstateを作れないようにするため)。
    """
    if attempted == 0:
        return MutationState.NO_MUTATION
    if unknown > 0:
        return MutationState.MUTATION_STATE_UNKNOWN
    if succeeded == attempted:
        return MutationState.MUTATION_SUCCEEDED
    if succeeded > 0 and failed > 0:
        return MutationState.PARTIAL_MUTATION
    if succeeded == 0 and failed > 0:
        return MutationState.MUTATION_FAILED
    # attempted == succeeded+failed+unknown が呼び出し側(__post_init__)で
    # 保証されているため、ここには到達しないはずだが、fail-closedのため防御する。
    raise ValueError(
        "Cannot derive a mutation state from "
        f"attempted={attempted}, succeeded={succeeded}, failed={failed}, unknown={unknown}."
    )


@dataclass(frozen=True)
class MutationSummary:
    """Error Contract v2の``mutation``objectを表す、immutableかつ自己検証済みのmodel。

    ``state`` はコンストラクタ引数ではなく、attempted/succeeded/failed/unknownから
    導出される(dataclassのinit=Falseフィールドとして計算・設定する)。
    ``recovery_action`` はcallerが明示する(例: MUTATION_FAILEDから
    RETRY_ALLOWEDへの自動推論は行わない――再実行の安全性は各commandの
    contractが個別に証明する責任を持つ)。ただし導出されたstateと矛盾する
    recovery_actionはコンストラクタで拒否する。

    invalid state/count/recovery/operationの組み合わせはインスタンス化できない
    (fail-closed: 例外を送出し、無効な状態をserializationしない)。
    """

    attempted: int
    succeeded: int
    failed: int
    unknown: int
    recovery_action: str
    operations: tuple[MutationOperation, ...] = ()
    state: str = field(init=False)

    def __post_init__(self) -> None:
        for name, value in (
            ("attempted", self.attempted),
            ("succeeded", self.succeeded),
            ("failed", self.failed),
            ("unknown", self.unknown),
        ):
            if value < 0:
                raise ValueError(f"MutationSummary.{name} must be >= 0, got {value}.")

        if self.attempted != self.succeeded + self.failed + self.unknown:
            raise ValueError(
                "MutationSummary count invariant violated: attempted must equal "
                "succeeded+failed+unknown "
                f"(attempted={self.attempted}, succeeded={self.succeeded}, "
                f"failed={self.failed}, unknown={self.unknown})."
            )

        state = _derive_state(self.attempted, self.succeeded, self.failed, self.unknown)
        object.__setattr__(self, "state", state)

        if self.recovery_action not in _RECOVERY_ACTIONS:
            raise ValueError(
                f"MutationSummary.recovery_action must be one of {sorted(_RECOVERY_ACTIONS)}, "
                f"got {self.recovery_action!r}."
            )
        allowed = _ALLOWED_RECOVERY_BY_STATE[state]
        if self.recovery_action not in allowed:
            raise ValueError(
                f"recovery_action {self.recovery_action!r} is not allowed for "
                f"state {state!r} (allowed: {sorted(allowed)})."
            )

        # operation detailのstateはsummaryのcount(0/非0)と矛盾してはいけない
        # (例: summary.unknown==0なのにoperationsにstate="unknown"が含まれる、は拒否)。
        available_states = {
            "failed": self.failed,
            "unknown": self.unknown,
        }
        for operation in self.operations:
            summary_count = available_states.get(operation.state, 0)
            if summary_count == 0:
                raise ValueError(
                    f"MutationOperation with state={operation.state!r} present but "
                    f"MutationSummary.{operation.state}=={summary_count} (key={operation.key!r})."
                )

    def to_dict(self) -> dict:
        """Error Contract v2の``mutation``フィールド用dict表現を返す。

        ``operations`` が空の場合はキー自体を省略する(成功だけのバッチや
        NO_MUTATIONではペイロードを不必要に膨らませない)。
        """
        payload: dict = {
            "state": self.state,
            "attempted": self.attempted,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "unknown": self.unknown,
            "recovery_action": self.recovery_action,
        }
        if self.operations:
            payload["operations"] = [op.to_dict() for op in self.operations]
        return payload
