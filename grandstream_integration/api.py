import frappe
from werkzeug.exceptions import RequestedRangeNotSatisfiable
from werkzeug.wrappers import Response

from grandstream_integration.ucm_api import fetch_recording

# The browser asks for the file again (in byte ranges) every time the user seeks.
# Keeping the recording a few minutes avoids a new UCM login + download per seek.
CACHE_SECONDS = 600


@frappe.whitelist()
def get_recording_url(call_log_name: str):
	"""Stream a Grandstream recording to the call page player (replaces the Twilio endpoint)."""
	if not call_log_name or not frappe.db.exists("CRM Call Log", call_log_name):
		frappe.throw("Call log not found", frappe.DoesNotExistError)

	log = frappe.get_doc("CRM Call Log", call_log_name)
	# Only users who can open this call may listen to it.
	log.check_permission("read")

	# Not a Grandstream call: hand it to the original CRM endpoint.
	if log.telephony_medium != "Grandstream":
		from crm.integrations.api import get_recording_url as crm_get_recording_url

		return crm_get_recording_url(call_log_name)

	filename = log.get("recording_url")
	if not filename:
		frappe.throw("Recording URL/Filename not found", frappe.DoesNotExistError)

	audio = _get_audio(filename)

	response = Response(audio, mimetype="audio/wav")  # Grandstream records wav
	response.headers["Content-Disposition"] = f'inline; filename="{filename}"'
	response.headers["Cache-Control"] = f"private, max-age={CACHE_SECONDS}"
	# Real seek support: answers "Range: bytes=..." with 206 + only the requested bytes,
	# instead of sending the whole file again for every seek.
	try:
		return response.make_conditional(
			frappe.request.environ, accept_ranges=True, complete_length=len(audio)
		)
	except RequestedRangeNotSatisfiable:
		return Response(status=416, headers={"Content-Range": f"bytes */{len(audio)}"})


def _get_audio(filename):
	key = f"grandstream_recording::{filename}"
	cache = frappe.cache()
	audio = cache.get_value(key)
	if audio:
		return audio

	try:
		audio = fetch_recording(filename)
	except Exception:
		frappe.log_error(title="Grandstream Recording Proxy Error", message=frappe.get_traceback())
		frappe.throw("Error communicating with Grandstream server.")

	cache.set_value(key, audio, expires_in_sec=CACHE_SECONDS)
	return audio
