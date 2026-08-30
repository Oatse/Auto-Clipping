from __future__ import annotations

from io import BytesIO
from pathlib import Path

from fastapi import UploadFile

from web.services.upload_helpers import UploadTooLargeError, save_upload_streaming


async def test_save_upload_streaming_rejects_file_over_limit(tmp_path: Path) -> None:
    upload = UploadFile(filename="big.mp4", file=BytesIO(b"abcdef"))

    try:
        await save_upload_streaming(
            upload,
            tmp_path / "big.mp4",
            chunk_bytes=2,
            max_bytes=5,
        )
    except UploadTooLargeError as exc:
        assert "Upload exceeds limit" in str(exc)
    else:
        raise AssertionError("Expected UploadTooLargeError")


async def test_save_upload_streaming_allows_oversize_when_limit_disabled(
    tmp_path: Path,
) -> None:
    upload = UploadFile(filename="big.mp4", file=BytesIO(b"abcdef"))
    destination = tmp_path / "big.mp4"

    written = await save_upload_streaming(
        upload,
        destination,
        chunk_bytes=2,
        max_bytes=0,
    )

    assert written == 6
    assert destination.read_bytes() == b"abcdef"
