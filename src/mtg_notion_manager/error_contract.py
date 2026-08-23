"""Stable public error contract for ``--error-json`` CLI output (schema_version 1).

Only ``import-article`` and ``apply-single-title-update`` participate in this
pilot. ``error_category``/``error_code`` are the stable, machine-parsable
public contract; ``message`` is a human-readable diagnostic and is NOT stable
(scripts must not parse it). This module never derives a category/code by
parsing ``str(exc)`` — classification is always based on the exception's
Python type (a typed discriminator), never on message text.
"""

from __future__ import annotations

import json
import sys

from mtg_notion_manager.config import ConfigError
from mtg_notion_manager.exceptions import (
    AmbiguousCardMatchError,
    CardMatchOverrideError,
    DeckCardValidationError,
    DeckCountMismatchError,
    DeckPageMappingConfigError,
    FetchError,
    MappingError,
    MtgNotionManagerError,
    MultipleDecksFoundError,
    NotionAPIError,
    ParseError,
    UnsupportedSourceError,
)
from mtg_notion_manager.services.single_card_title_update import (
    SingleUpdateConfigError,
    SingleUpdateGuardError,
)
from mtg_notion_manager.services.title_update_dry_run import TitleUpdateManifestConfigError

SCHEMA_VERSION = 1


class ErrorCategory:
    """Accepted v1 public error-category vocabulary."""

    CONFIGURATION = "CONFIGURATION"
    INPUT_VALIDATION = "INPUT_VALIDATION"
    PRECONDITION = "PRECONDITION"
    MAPPING = "MAPPING"
    IDENTITY_AMBIGUITY = "IDENTITY_AMBIGUITY"
    CONFLICT = "CONFLICT"
    INTEGRITY = "INTEGRITY"
    EXTERNAL_SOURCE = "EXTERNAL_SOURCE"
    PRODUCTION_API = "PRODUCTION_API"
    INTERNAL = "INTERNAL"


class ErrorCode:
    """Stable public error codes implemented by the pilot commands."""

    CONFIG_LOAD_FAILED = "CONFIG_LOAD_FAILED"
    ARTICLE_FETCH_FAILED = "ARTICLE_FETCH_FAILED"
    ARTICLE_PARSE_FAILED = "ARTICLE_PARSE_FAILED"
    UNSUPPORTED_SOURCE = "UNSUPPORTED_SOURCE"
    MULTIPLE_DECKS_FOUND = "MULTIPLE_DECKS_FOUND"
    UNMAPPED_VALUE = "UNMAPPED_VALUE"
    NOTION_API_ERROR = "NOTION_API_ERROR"
    CARD_VALIDATION_FAILED = "CARD_VALIDATION_FAILED"
    DECK_COUNT_MISMATCH = "DECK_COUNT_MISMATCH"
    AMBIGUOUS_CARD_MATCH = "AMBIGUOUS_CARD_MATCH"
    CARD_OVERRIDE_CONFIG_INVALID = "CARD_OVERRIDE_CONFIG_INVALID"
    DECK_PAGE_MAP_CONFIG_INVALID = "DECK_PAGE_MAP_CONFIG_INVALID"
    SINGLE_UPDATE_CONFIG_INVALID = "SINGLE_UPDATE_CONFIG_INVALID"
    MANIFEST_INVALID = "MANIFEST_INVALID"
    WRITE_GUARD_REJECTED = "WRITE_GUARD_REJECTED"
    EXPECTED_COUNT_NOT_ONE = "EXPECTED_COUNT_NOT_ONE"
    MAX_UPDATES_NOT_ONE = "MAX_UPDATES_NOT_ONE"
    PREFLIGHT_NOT_ELIGIBLE = "PREFLIGHT_NOT_ELIGIBLE"
    APPROVAL_DIGEST_MISMATCH = "APPROVAL_DIGEST_MISMATCH"
    OPTIMISTIC_LOCK_MISMATCH = "OPTIMISTIC_LOCK_MISMATCH"
    POST_VERIFICATION_FAILED = "POST_VERIFICATION_FAILED"
    UNCLASSIFIED_DOMAIN_ERROR = "UNCLASSIFIED_DOMAIN_ERROR"
    UNHANDLED_EXCEPTION = "UNHANDLED_EXCEPTION"


# Exception type -> (error_category, error_code). Checked in order; a subtype
# must be listed before any of its base classes. The final entry is the
# fallback for any other MtgNotionManagerError subtype without a specific
# mapping (e.g. one not yet reachable on the two pilot command paths).
_EXCEPTION_CLASSIFICATION: tuple[tuple[type[Exception], str, str], ...] = (
    (ConfigError, ErrorCategory.CONFIGURATION, ErrorCode.CONFIG_LOAD_FAILED),
    (FetchError, ErrorCategory.EXTERNAL_SOURCE, ErrorCode.ARTICLE_FETCH_FAILED),
    (ParseError, ErrorCategory.EXTERNAL_SOURCE, ErrorCode.ARTICLE_PARSE_FAILED),
    (UnsupportedSourceError, ErrorCategory.EXTERNAL_SOURCE, ErrorCode.UNSUPPORTED_SOURCE),
    (MultipleDecksFoundError, ErrorCategory.IDENTITY_AMBIGUITY, ErrorCode.MULTIPLE_DECKS_FOUND),
    (MappingError, ErrorCategory.MAPPING, ErrorCode.UNMAPPED_VALUE),
    (DeckCardValidationError, ErrorCategory.INPUT_VALIDATION, ErrorCode.CARD_VALIDATION_FAILED),
    (DeckCountMismatchError, ErrorCategory.INPUT_VALIDATION, ErrorCode.DECK_COUNT_MISMATCH),
    (AmbiguousCardMatchError, ErrorCategory.IDENTITY_AMBIGUITY, ErrorCode.AMBIGUOUS_CARD_MATCH),
    (CardMatchOverrideError, ErrorCategory.CONFIGURATION, ErrorCode.CARD_OVERRIDE_CONFIG_INVALID),
    (
        DeckPageMappingConfigError,
        ErrorCategory.CONFIGURATION,
        ErrorCode.DECK_PAGE_MAP_CONFIG_INVALID,
    ),
    (SingleUpdateGuardError, ErrorCategory.INTEGRITY, ErrorCode.WRITE_GUARD_REJECTED),
    (
        SingleUpdateConfigError,
        ErrorCategory.CONFIGURATION,
        ErrorCode.SINGLE_UPDATE_CONFIG_INVALID,
    ),
    (TitleUpdateManifestConfigError, ErrorCategory.CONFIGURATION, ErrorCode.MANIFEST_INVALID),
    (NotionAPIError, ErrorCategory.PRODUCTION_API, ErrorCode.NOTION_API_ERROR),
    (MtgNotionManagerError, ErrorCategory.INTERNAL, ErrorCode.UNCLASSIFIED_DOMAIN_ERROR),
)


def classify_exception(exc: BaseException) -> tuple[str, str]:
    """Return ``(error_category, error_code)`` from ``exc``'s Python type only.

    Never inspects ``str(exc)``. Any expected domain error (an
    ``MtgNotionManagerError``/``ConfigError`` subtype) without a specific
    mapping falls back to ``INTERNAL``/``UNCLASSIFIED_DOMAIN_ERROR``. Anything
    outside that hierarchy falls back to ``INTERNAL``/``UNHANDLED_EXCEPTION``.
    """
    for exc_type, category, code in _EXCEPTION_CLASSIFICATION:
        if isinstance(exc, exc_type):
            return category, code
    return ErrorCategory.INTERNAL, ErrorCode.UNHANDLED_EXCEPTION


def emit_error_json(command: str, category: str, code: str, message: str) -> None:
    """Write exactly one pure JSON object to stdout for ``--error-json`` mode.

    No Rich rendering, no ANSI, no surrounding prose. Field ordering is not
    part of the public contract.
    """
    payload = {
        "schema_version": SCHEMA_VERSION,
        "command": command,
        "error_category": category,
        "error_code": code,
        "message": message,
    }
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
