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

Error Contract v2 output is a deliberate two-layer structure, and
``recovery_action`` belongs entirely to the second layer:

- Layer A — command failure (``error_category``/``error_code``/``message``,
  defined in ``error_contract.py``): *why did the command not complete
  normally?* This is the top-level domain-error classification (e.g. an
  ``IDENTITY_AMBIGUITY``/card-resolution failure that aborted a batch).
- Layer B — mutation state (everything in this module): *what happened to
  the Notion writes that were actually attempted, and what must be done
  specifically because of THAT mutation state?*

``recovery_action`` (and every other field on ``MutationSummary``) answers
Layer B only. It never describes, overrides, or substitutes for how to
resolve the command's top-level domain error — a consumer must always
inspect ``error_category``/``error_code`` independently of
``mutation.recovery_action``. In particular, ``recovery_action == NONE``
means only "no action is required specifically to reconcile Notion mutation
state" — it is not a claim that the command as a whole succeeded or needs
no follow-up (see the ``RecoveryAction`` docstring below for the case where
a batch aborts on a domain error after some writes had already succeeded
cleanly: ``mutation.state`` can legitimately be ``MUTATION_SUCCEEDED`` with
``recovery_action: NONE`` while the surrounding command still failed).
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
    """What a machine consumer must do to recover/reconcile the reported
    *Notion mutation state* specifically — never auto-inferred from the
    mutation state alone beyond what :data:`_ALLOWED_RECOVERY_BY_STATE`
    permits (a command's own contract must justify ``RETRY_ALLOWED``).

    Scope (frozen by this Work Unit — see the module docstring's Layer A/B
    split): every value here describes *only* what, if anything, must be
    done because of the mutation outcome itself. None of these values
    describe, replace, or imply anything about resolving the command's
    top-level domain error (``error_category``/``error_code``). A consumer
    that only checks ``recovery_action`` and skips the top-level
    classification is misusing this field.
    """

    #: No action is required *specifically to reconcile Notion mutation
    #: state* (valid only when every attempted write's outcome is fully and
    #: successfully known: NO_MUTATION or MUTATION_SUCCEEDED). This is NOT a
    #: claim that "no action is required" in general, and NOT a claim that
    #: the command succeeded — the same response can carry a NONE mutation
    #: alongside a top-level error_category/error_code that still needs
    #: resolving (e.g. a card-identity error that aborted the batch after
    #: every write attempted so far had already succeeded).
    NONE = "NONE"
    #: The mutation contract has independently established that another
    #: write attempt is safe without prior reconciliation (valid only for
    #: MUTATION_FAILED, where every failure is definitively known —
    #: never for MUTATION_STATE_UNKNOWN). No current command emits this
    #: value; rerun safety has not yet been proven for any of them
    #: (see import_cards_mutation_adapter.py, which deliberately never
    #: selects this value pending a dedicated rerun-safety Work Unit).
    RETRY_ALLOWED = "RETRY_ALLOWED"
    #: One or more attempted writes have an unknown completion state (or,
    #: depending on a command's own contract, a mix of known outcomes where
    #: reconciling live Notion state is preferred to immediate manual
    #: escalation). Reconcile current Notion state before any retry —
    #: never retry blindly on the strength of this value alone.
    RECONCILE_BEFORE_RETRY = "RECONCILE_BEFORE_RETRY"
    #: Every attempted write's outcome is definitively known (no UNKNOWN
    #: completions), but at least one write is a known failure and
    #: automatic retry/recovery safety has not been established for that
    #: mutation pattern. A human must review the mutation results
    #: (``mutation.operations``) before another write attempt. This is a
    #: statement about the *mutation*, not about the command's domain
    #: error — do not read it as "the whole command needs review" (though
    #: in practice a command that reports this will usually also have a
    #: reason to require attention at the top level too).
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

    scope(このモジュールのdocstring・RecoveryActionのdocstring参照): `state`と
    `recovery_action`はどちらもmutationそのものについての事実・指示であり、
    このcommandがtop-levelでなぜ失敗したか(error_category/error_code)を
    表すものではない。`recovery_action == RecoveryAction.NONE`は
    「mutation側の追加対応は不要」を意味するだけで、「command全体が成功した」
    ことを意味しない――例えば書き込みがすべて成功した直後にcard identityの
    問題でbatchが中断した場合、mutation側はMUTATION_SUCCEEDED/NONEのまま、
    top-levelのerror_category/error_codeは依然として実際の中断原因を表す。
    """

    attempted: int
    succeeded: int
    failed: int
    unknown: int
    #: このmutationの状態を解消するために必要な対応(RecoveryAction参照)。
    #: command自体のtop-level domain error解消を代替するものではない。
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
