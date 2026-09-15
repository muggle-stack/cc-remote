"""Explicit local attachment reads using the wrapper's shared validation."""

import base64
import mimetypes
import os
from pathlib import Path
import stat

from cc_remote.attachments import (
    MAX_SINGLE_ATTACHMENT_BYTES,
    validate_attachments,
)


def read_attachment(path: str, *, image: bool = False) -> dict:
    source = Path(path).expanduser()
    fd = os.open(source, os.O_RDONLY | os.O_NONBLOCK)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise ValueError("An attachment must be a regular file")
    with os.fdopen(fd, "rb") as stream:
        raw = stream.read(MAX_SINGLE_ATTACHMENT_BYTES + 1)
    if len(raw) > MAX_SINGLE_ATTACHMENT_BYTES:
        raise ValueError("One attachment exceeds the 6 MiB limit")
    data = base64.b64encode(raw).decode("ascii")
    content = (
        {"media_type": mimetypes.guess_type(source.name)[0] or "", "data": data}
        if image
        else {"filename": source.name, "data": data}
    )
    error = validate_attachments(
        [content] if image else None, None if image else [content]
    )
    if error:
        raise ValueError(error)
    return {"name": source.name, "image": image, "content": content}
