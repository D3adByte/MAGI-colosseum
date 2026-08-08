from __future__ import annotations


class ColosseumError(Exception):
    code = "internal_error"
    retryable = False

    def as_dict(self) -> dict:
        return {"code": self.code, "message": str(self), "retryable": self.retryable}


class NotFoundError(ColosseumError):
    code = "not_found"


class ValidationError(ColosseumError):
    code = "validation_failed"


class BuildError(ColosseumError):
    code = "build_failed"


class StartError(ColosseumError):
    code = "start_failed"


class ReadinessTimeout(ColosseumError):
    code = "readiness_timeout"
