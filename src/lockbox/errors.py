"""Errors returned by the Lockbox HTTP API."""

import httpx


class LockboxHttpError(Exception):
    """HTTP failure with a readable message and the original response."""

    def __init__(self, response: httpx.Response, method: str, path: str):
        self.response = response
        self.status_code = response.status_code
        self.body = response.text
        message = f"{method} {path}: HTTP {self.status_code} {response.reason_phrase}"
        if self.body:
            message += f"\n{self.body}"
        super().__init__(message)
