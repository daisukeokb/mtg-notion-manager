"""Generation Workspace: multi-file atomic commit mechanism.

Implements the Generation Directory + Single Atomic Active Generation
Switch design (WP-CLAIM-EXIT Multi-file Atomic Commit Mechanism Design).

This package is self-contained and has no dependency on mtg_notion_manager.
It is not part of the installable ``mtg-notion-manager`` distribution.
"""

from .bootstrap import BootstrapResult, bootstrap_generation_workspace
from .digest import compute_generation_digest
from .resolver import (
    ResolveResult,
    VerifyResult,
    resolve_active_generation,
    verify_active_generation,
)
from .transaction import (
    BeginResult,
    CommitResult,
    RecoveryResult,
    begin_generation_transaction,
    commit_generation_transaction,
    recover_generation_transaction,
)

__all__ = [
    "BootstrapResult",
    "bootstrap_generation_workspace",
    "compute_generation_digest",
    "ResolveResult",
    "VerifyResult",
    "resolve_active_generation",
    "verify_active_generation",
    "BeginResult",
    "CommitResult",
    "RecoveryResult",
    "begin_generation_transaction",
    "commit_generation_transaction",
    "recover_generation_transaction",
]
