"""Domain errors with stable machine-readable codes (mapped to the API error contract)."""

from __future__ import annotations


class MagicImportError(Exception):
    code = "internal_error"
    status_code = 500

    def __init__(self, message: str, details: list | None = None, code: str | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or []
        if code:
            self.code = code


class NotFoundError(MagicImportError):
    code = "not_found"
    status_code = 404


class InvalidInputError(MagicImportError):
    code = "validation_error"
    status_code = 422


class InvalidFileError(MagicImportError):
    code = "invalid_file"
    status_code = 400


class FileTooLargeError(MagicImportError):
    code = "file_too_large"
    status_code = 413


class InvalidMappingError(MagicImportError):
    code = "invalid_mapping"
    status_code = 422


class InvalidStateError(MagicImportError):
    code = "invalid_state"
    status_code = 409


class ConflictError(MagicImportError):
    code = "conflict"
    status_code = 409
