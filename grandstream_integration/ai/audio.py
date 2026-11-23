"""
Shrink UCM recordings before sending them to Gemini.

UCM records 8 kHz wav (about 1 MB a minute). A mono 32 kbps mp3 is about 240 KB a minute,
so even a one-hour call stays under Gemini's inline request limit.
If ffmpeg is missing or fails, the original wav is sent as-is.
"""

import shutil
import subprocess

# Gemini rejects inline requests above ~20 MB; keep a margin for the prompt.
MAX_INLINE_BYTES = 18 * 1024 * 1024


def to_small_mp3(audio: bytes) -> tuple[bytes, str]:
	"""Return (audio_bytes, mime_type)."""
	if shutil.which("ffmpeg"):
		try:
			result = subprocess.run(
				[
					"ffmpeg",
					"-hide_banner",
					"-loglevel",
					"error",
					"-i",
					"pipe:0",
					"-ac",
					"1",
					"-ar",
					"16000",
					"-b:a",
					"32k",
					"-f",
					"mp3",
					"pipe:1",
				],
				input=audio,
				capture_output=True,
				timeout=120,
				check=True,
			)
			if result.stdout:
				audio, mime = result.stdout, "audio/mpeg"
				return _check_size(audio), mime
		except (subprocess.SubprocessError, OSError):
			pass

	return _check_size(audio), "audio/wav"


def _check_size(audio: bytes) -> bytes:
	if len(audio) > MAX_INLINE_BYTES:
		raise ValueError(
			f"Recording is {len(audio) // (1024 * 1024)} MB after compression, "
			f"above the {MAX_INLINE_BYTES // (1024 * 1024)} MB inline limit."
		)
	return audio
