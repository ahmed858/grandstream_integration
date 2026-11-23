"""
AI call analysis for Grandstream calls.

Flow (one background job per call):
    1. download the recording from the UCM and shrink it with ffmpeg
    2. Gemini call 1: audio -> transcript labelled المسوق / العميل
    3. Gemini call 2: transcript -> CallAnalysis (structured output)
    4. save the result on CRM Call Log (custom_ai_* fields) + a short note on the Lead/Deal

The AI only writes a report: it never changes the lead status or creates tasks.

Safe testing on production, from `bench --site <site> console`:
    from grandstream_integration.ai.analysis import analyze_call, analyze_recent
    analyze_call("<CRM Call Log name>", dry_run=True)   # prints, writes nothing
    analyze_recent(20)                                   # writes only the AI fields
Automatic analysis runs only when "AI Enabled" is ticked in Grandstream Settings.
"""
import json
import time

import frappe

from grandstream_integration.ai import prompts
from grandstream_integration.ai.schemas import CallAnalysis

DEFAULT_SETTINGS_NAME = "5gs6lirpf6"
DEFAULT_MODEL = "gemini-3.5-flash"
DEFAULT_MIN_DURATION = 20
MAX_ATTEMPTS = 3
DOCTYPE = "CRM Call Log"


# ── Settings ────────────────────────────────────────────────────────────────

def _settings():
    from grandstream_integration.utils import _get_settings

    return _get_settings(DEFAULT_SETTINGS_NAME)


def _min_duration(settings):
    return settings.get("ai_min_duration") or DEFAULT_MIN_DURATION


def _set(call_log_name, values):
    """Write AI fields directly, without running CRM Call Log hooks or touching `modified`."""
    frappe.db.set_value(DOCTYPE, call_log_name, values, update_modified=False)


# ── Queueing ────────────────────────────────────────────────────────────────

def enqueue_analysis(call_log):
    """
    Called from the sync jobs when a call becomes Completed with a recording.
    Does nothing unless "AI Enabled" is ticked.
    """
    settings = _settings()
    if not settings.get("ai_enabled"):
        return
    if not call_log.get("recording_url"):
        return
    if (call_log.get("duration") or 0) < _min_duration(settings):
        _set(call_log.name, {"custom_ai_status": "Skipped"})
        return

    _set(call_log.name, {"custom_ai_status": "Pending"})
    _enqueue(call_log.name)


def _enqueue(call_log_name):
    frappe.enqueue(
        "grandstream_integration.ai.analysis.analyze_call",
        queue="long",
        timeout=900,
        job_id=f"grandstream-call-analysis::{call_log_name}",
        deduplicate=True,
        enqueue_after_commit=True,
        call_log_name=call_log_name,
    )


def retry_pending_analyses():
    """
    Hourly. Re-queues calls that failed (up to MAX_ATTEMPTS) or that stayed Pending
    for over two hours (worker restarted, Gemini quota reached, ...).
    """
    settings = _settings()
    if not settings.get("ai_enabled"):
        return

    failed = frappe.get_all(
        DOCTYPE,
        filters={
            "telephony_medium": "Grandstream",
            "custom_ai_status": "Failed",
            "custom_ai_attempts": ["<", MAX_ATTEMPTS],
        },
        pluck="name",
        limit=50,
    )
    stuck = frappe.get_all(
        DOCTYPE,
        filters={
            "telephony_medium": "Grandstream",
            "custom_ai_status": ["in", ["Pending", "Processing"]],
            "modified": ["<", frappe.utils.add_to_date(frappe.utils.now_datetime(), hours=-2)],
        },
        pluck="name",
        limit=50,
    )
    for name in set(failed + stuck):
        _enqueue(name)


# ── The analysis itself ─────────────────────────────────────────────────────

def analyze_call(call_log_name, dry_run=False):
    """
    Analyse one call. With dry_run=True nothing is written to the database:
    the result is printed and returned so it can be reviewed first.
    """
    log = frappe.get_doc(DOCTYPE, call_log_name)
    settings = _settings()
    model = settings.get("gemini_model") or DEFAULT_MODEL

    if not dry_run:
        _set(call_log_name, {"custom_ai_status": "Processing"})
        frappe.db.commit()

    trace = _start_trace(settings, call_log_name)
    try:
        transcript, result = _run(log, settings, model)
    except Exception as e:
        _end_trace(trace, "Error")
        if dry_run:
            raise
        _handle_failure(log, e)
        return None
    _end_trace(trace, "Success")

    output = {"transcript": transcript, "analysis": result.model_dump()}
    if dry_run:
        print(transcript)
        print(json.dumps(output["analysis"], ensure_ascii=False, indent=2))
        return output

    _save(log, transcript, result, model)
    if settings.get("ai_add_note") and log.get("reference_docname"):
        _add_note(log, result)
    frappe.db.commit()
    return output


def _run(log, settings, model):
    from langchain_core.messages import HumanMessage, SystemMessage
    from langchain_google_genai import ChatGoogleGenerativeAI

    from grandstream_integration.ai.audio import to_small_mp3
    from grandstream_integration.ucm_api import fetch_recording

    api_key = settings.get_password("gemini_api_key", raise_exception=False)
    if not api_key:
        raise ValueError("Gemini API Key is empty in Grandstream Settings.")
    if not log.get("recording_url"):
        raise ValueError("This call has no recording.")

    llm = ChatGoogleGenerativeAI(
        model=model,
        google_api_key=api_key,
        temperature=0,
        max_retries=2,
    )

    # 1) audio -> transcript
    audio, mime_type = to_small_mp3(fetch_recording(log.recording_url, settings))
    transcript = _text(
        llm.invoke(
            [
                HumanMessage(
                    content=[
                        {"type": "text", "text": prompts.TRANSCRIBE},
                        {"type": "media", "mime_type": mime_type, "data": audio},
                    ]
                )
            ]
        )
    ).strip()
    if not transcript:
        raise ValueError("Gemini returned an empty transcript.")

    # 2) transcript -> structured analysis
    # json_schema sends the full schema (with the nested score/objection objects) as
    # response_json_schema; the default function-calling mode drops nested models.
    analyzer = llm.with_structured_output(CallAnalysis, method="json_schema")
    result = analyzer.invoke(
        [
            SystemMessage(content=prompts.system(settings.get("company_context"), log.type)),
            HumanMessage(
                content=prompts.analysis_input(
                    log.type, transcript, reference=_reference_label(log), duration=log.get("duration")
                )
            ),
        ]
    )
    return transcript, _normalize(result, log.type)


def _text(message):
    """Plain text of a LangChain AI message, whatever content format the model returned."""
    text = getattr(message, "text", None)
    if callable(text) and not isinstance(text, str):  # older langchain-core: .text() was a method
        text = text()
    if isinstance(text, str) and text:
        return text
    content = message.content
    if isinstance(content, str):
        return content
    return "".join(p.get("text", "") for p in content if isinstance(p, dict))


def _normalize(result, call_type):
    """Clamp scores to 1-5, keep only the known criteria in their order, cap coaching at 3."""
    if isinstance(result, dict):
        result = CallAnalysis.model_validate(result)

    by_name = {s.criterion.strip(): s for s in result.scores}
    ordered = []
    for name, _desc in prompts.criteria_for(call_type):
        score = by_name.get(name)
        if score:
            score.criterion = name
            score.score = max(1, min(5, int(score.score)))
            ordered.append(score)
    result.scores = ordered or result.scores
    result.coaching = [c for c in result.coaching if c.strip()][:3]
    if result.interested == "مهتم":
        result.not_interested_check = None
    return result


def _reference_label(log):
    if not log.get("reference_doctype") or not log.get("reference_docname"):
        return None
    label = f"{log.reference_doctype} {log.reference_docname}"
    status = frappe.db.get_value(log.reference_doctype, log.reference_docname, "status")
    return f"{label} (الحالة: {status})" if status else label


def _marketer(log):
    """The user who handled the call: caller for outgoing, receiver / extension owner for incoming."""
    if log.type == "Outgoing" and log.get("caller"):
        return log.caller
    if log.get("receiver"):
        return log.receiver
    extension = log.get("to") if log.type == "Incoming" else log.get("from")
    if extension:
        return frappe.db.get_value("User Extension", {"extension": extension}, "user")
    return None


def _save(log, transcript, result, model):
    scores = [s.model_dump() for s in result.scores]
    overall = round(sum(s["score"] for s in scores) / len(scores), 2) if scores else None

    _set(
        log.name,
        {
            "custom_ai_status": "Done",
            "custom_ai_error": None,
            "custom_ai_marketer": _marketer(log),
            "custom_ai_summary": result.summary,
            "custom_ai_service": result.service,
            "custom_ai_customer_type": result.customer_type,
            "custom_ai_interested": result.interested,
            "custom_ai_next_step": (result.next_step or "")[:140] or None,
            "custom_ai_next_step_date": _valid_date(result.next_step_date),
            "custom_ai_transcript": transcript,
            "custom_ai_success": 1 if result.success else 0,
            "custom_ai_overall_score": overall,
            "custom_ai_scores": json.dumps(scores, ensure_ascii=False),
            "custom_ai_customer_profile": result.customer_profile,
            "custom_ai_not_interested_check": result.not_interested_check,
            "custom_ai_objections": json.dumps(
                [o.model_dump() for o in result.objections], ensure_ascii=False
            ),
            "custom_ai_coaching": "\n".join(f"- {c}" for c in result.coaching),
            "custom_ai_model": model,
            "custom_ai_analyzed_on": frappe.utils.now_datetime(),
        },
    )


def _valid_date(value):
    try:
        return frappe.utils.getdate(value).isoformat() if value else None
    except Exception:
        return None


def _add_note(log, result):
    """A neutral summary on the Lead/Deal timeline. No scores: everyone on the lead can see it."""
    lines = [
        f"<p>{frappe.utils.escape_html(result.summary)}</p>",
        f"<p><b>اهتمام العميل:</b> {result.interested}</p>",
    ]
    if result.next_step:
        lines.append(f"<p><b>الخطوة الجاية:</b> {frappe.utils.escape_html(result.next_step)}</p>")
    try:
        frappe.get_doc(
            {
                "doctype": "FCRM Note",
                "title": "ملخص المكالمة (AI)",
                "content": "".join(lines),
                "reference_doctype": log.reference_doctype,
                "reference_docname": log.reference_docname,
            }
        ).insert(ignore_permissions=True)
    except Exception:
        # The report on the call log is what matters; a failed note must not fail the job.
        frappe.log_error(title=f"Call analysis: note failed for {log.name}", message=frappe.get_traceback())


def _handle_failure(log, error):
    message = str(error)
    if _is_quota_error(message):
        # Free-tier limit reached: keep it Pending, the hourly retry picks it up. Not an attempt.
        _set(log.name, {"custom_ai_status": "Pending", "custom_ai_error": message[:500]})
    else:
        _set(
            log.name,
            {
                "custom_ai_status": "Failed",
                "custom_ai_error": message[:500],
                "custom_ai_attempts": (log.get("custom_ai_attempts") or 0) + 1,
            },
        )
        frappe.log_error(title=f"Call analysis failed: {log.name}", message=frappe.get_traceback())
    frappe.db.commit()


def _is_quota_error(message):
    return "429" in message or "RESOURCE_EXHAUSTED" in message or "quota" in message.lower()


# ── Manual run for testing ──────────────────────────────────────────────────

def analyze_recent(limit=20, dry_run=False, pause_seconds=8):
    """
    Analyse the latest completed Grandstream calls that have a recording and were not analysed yet.
    Runs in the console (not in the background), works even when "AI Enabled" is off.
    The pause keeps us under the free-tier requests-per-minute limit (2 requests per call).
    """
    settings = _settings()
    names = frappe.get_all(
        DOCTYPE,
        filters={
            "telephony_medium": "Grandstream",
            "status": "Completed",
            "recording_url": ["is", "set"],
            "duration": [">=", _min_duration(settings)],
            "custom_ai_status": ["not in", ["Done"]],
        },
        order_by="creation desc",
        pluck="name",
        limit=limit,
    )
    print(f"Analysing {len(names)} calls (dry_run={dry_run})")
    done = 0
    for i, name in enumerate(names, 1):
        output = _safe_dry_run(name) if dry_run else analyze_call(name)
        if output:
            done += 1
            score = _overall(output)
            print(f"[{i}/{len(names)}] {name}: {output['analysis']['interested']} | score {score}")
        else:
            print(f"[{i}/{len(names)}] {name}: failed (see custom_ai_error / Error Log)")
        if i < len(names):
            time.sleep(pause_seconds)
    print(f"Done: {done}/{len(names)}")


def _safe_dry_run(name):
    try:
        return analyze_call(name, dry_run=True)
    except Exception as e:
        print(f"{name}: {e}")
        return None


def _overall(output):
    scores = [s["score"] for s in output["analysis"]["scores"]]
    return round(sum(scores) / len(scores), 2) if scores else "-"


# ── AgentOps (optional) ─────────────────────────────────────────────────────

_agentops_ready = False


def _start_trace(settings, call_log_name):
    """One AgentOps trace per call. Any AgentOps problem is ignored: tracing must never break analysis."""
    global _agentops_ready
    api_key = settings.get_password("agentops_api_key", raise_exception=False)
    if not api_key:
        return None
    try:
        import agentops

        if not _agentops_ready:
            agentops.init(api_key=api_key, auto_start_session=False, default_tags=["grandstream-calls"])
            _agentops_ready = True
        return agentops.start_trace(trace_name=f"call {call_log_name}", tags=[call_log_name])
    except Exception:
        return None


def _end_trace(trace, state):
    if not trace:
        return
    try:
        import agentops

        agentops.end_trace(trace, end_state=state)
    except Exception:
        pass
