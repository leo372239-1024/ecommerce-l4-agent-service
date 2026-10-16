from collections.abc import AsyncIterator

from fastapi import HTTPException, UploadFile


async def iter_upload_chunks(upload: UploadFile, max_file_bytes: int) -> AsyncIterator[bytes]:
    """Future ingestion writes to temporary storage and commits only after this finishes."""
    if upload.size is not None and upload.size > max_file_bytes:
        raise HTTPException(413, "File exceeds upload limit")
    total = 0
    while chunk := await upload.read(1024 * 1024):
        total += len(chunk)
        if total > max_file_bytes:
            raise HTTPException(413, "File exceeds upload limit")
        yield chunk
