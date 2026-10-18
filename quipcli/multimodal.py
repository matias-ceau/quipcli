import base64
import sys
from pathlib import Path
from urllib.parse import urlparse

from .constants import (
    _AUDIO_EXTS,
    _IMAGE_EXTS,
    _MAX_FILE_BYTES,
    _MEDIA_EXTENSIONS,
    _PDF_EXTS,
    _VIDEO_EXTS,
)


def _is_image_url(token: str) -> bool:
    if not token.startswith(("http://", "https://")):
        return False
    return Path(urlparse(token).path).suffix.lower() in _IMAGE_EXTS


def _encode_file_content(path: Path) -> dict:
    if path.stat().st_size > _MAX_FILE_BYTES:
        mb = path.stat().st_size // 1024 // 1024
        print(
            f"Warning: {path.name} is {mb}MB — may exceed API limits.", file=sys.stderr
        )
    ext = path.suffix.lower()
    mime = _MEDIA_EXTENSIONS.get(ext, "application/octet-stream")
    data = base64.b64encode(path.read_bytes()).decode()
    if ext in _IMAGE_EXTS:
        return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}}
    if ext in _PDF_EXTS:
        return {"type": "file", "file": {"filename": path.name, "data": data}}
    if ext in _AUDIO_EXTS:
        return {
            "type": "input_audio",
            "input_audio": {"data": data, "format": ext.lstrip(".")},
        }
    if ext in _VIDEO_EXTS:
        return {"type": "video_url", "video_url": {"url": f"data:{mime};base64,{data}"}}
    return {"type": "file", "file": {"filename": path.name, "data": data}}


def _build_user_content(
    words: list[str],
    extra_files: list[str] | None = None,
    stdin_text: str | None = None,
) -> tuple[str | list[dict], set[str]]:
    """
    Scan words and extra_files for media files/URLs.
    stdin_text (piped input) is appended after the word prompt, blank-line separated.
    Returns (content, detected_input_modalities).
    content is a plain str when text-only, list[dict] when multimodal.
    """
    text_tokens: list[str] = []
    media_parts: list[dict] = []
    detected: set[str] = set()

    def _add_file(path: Path) -> None:
        ext = path.suffix.lower()
        media_parts.append(_encode_file_content(path))
        if ext in _IMAGE_EXTS:
            detected.add("image")
        elif ext in _PDF_EXTS:
            detected.add("file")
        elif ext in _AUDIO_EXTS:
            detected.add("audio")
        elif ext in _VIDEO_EXTS:
            detected.add("video")

    for f in extra_files or []:
        p = Path(f)
        if p.exists():
            _add_file(p)
        else:
            print(f"Warning: input file not found: {f}", file=sys.stderr)

    for word in words:
        p = Path(word)
        if p.suffix.lower() in _MEDIA_EXTENSIONS and p.exists():
            _add_file(p)
        elif _is_image_url(word):
            media_parts.append({"type": "image_url", "image_url": {"url": word}})
            detected.add("image")
        else:
            text_tokens.append(word)

    prompt_text = " ".join(text_tokens)
    if stdin_text:
        prompt_text = f"{prompt_text}\n\n{stdin_text}" if prompt_text else stdin_text
    if not media_parts:
        return prompt_text, detected

    content: list[dict] = []
    if prompt_text:
        content.append({"type": "text", "text": prompt_text})
    content.extend(media_parts)
    return content, detected
