"""
UCM HTTP API Client
"""
import hashlib
import requests
import frappe

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def _get_settings(settings_name):
    return frappe.get_doc("Grandstream Settings", settings_name)


def _ucm_url(settings) -> str:
    return f"https://{settings.ucm_host}:{settings.ucm_port or 8089}/api"


def _md5(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()


def _login(settings) -> str | None:
    url = _ucm_url(settings)
    password = settings.get_password("ami_secret")

    resp = requests.post(url, json={
        "request": {"action": "challenge", "user": settings.ami_user, "version": "1.0"}
    }, verify=False, timeout=10)

    data = resp.json()
    if data.get("status") != 0:
        print(f"[UCM API] Challenge فشل: {data}")
        return None

    challenge = data["response"]["challenge"]
    token = _md5(challenge + password)

    resp = requests.post(url, json={
        "request": {"action": "login", "user": settings.ami_user, "token": token}
    }, verify=False, timeout=10)

    data = resp.json()
    if data.get("status") != 0:
        print(f"[UCM API] Login فشل: {data}")
        return None

    return data["response"]["cookie"]


def _logout(settings, cookie: str):
    try:
        requests.post(_ucm_url(settings), json={
            "request": {"action": "logout", "cookie": cookie}
        }, verify=False, timeout=5)
    except Exception:
        pass


def _request(settings, cookie: str, action: str, extra: dict = None) -> dict:
    payload = {"action": action, "cookie": cookie}
    if extra:
        payload.update(extra)
    resp = requests.post(_ucm_url(settings), json={"request": payload}, verify=False, timeout=10)
    return resp.json()


# ── Active Calls ─────────────────────────────────────────────────────────────

def get_active_calls() -> list[dict]:
    settings = _get_settings('5gs6lirpf6')
    cookie = _login(settings)
    if not cookie:
        return []

    try:
        bridged   = _request(settings, cookie, "listBridgedChannels")
        unbridged = _request(settings, cookie, "listUnBridgedChannels")

        active = []

        # bridged → {"response": {"channel": [...], ...}}
        for ch in (bridged.get("response", {}).get("channel") or []):
            active.append({
                "uniqueid": ch.get("uniqueid1") or ch.get("uniqueid2") or "",
                "caller":   ch.get("callerid1", ""),
                "callee":   ch.get("callerid2", ""),
                "status":   "In Progress",
            })


        for ch in (unbridged.get("response", {}).get("channel") or []):
            active.append({
                "uniqueid": ch.get("linkedid") or ch.get("uniqueid") or "",
                "caller":   ch.get("callernum", ""),
                "callee":   ch.get("connectednum", ""),
                "status":   "Ringing",
                "start":    ch.get('alloc_time'),
            })
 


        return active

    finally:
        _logout(settings, cookie)


# ── CDR Search ───────────────────────────────────────────────────────────────

def get_cdr_by_start_time(start_time: str, end_time: str = None) -> list:
    settings = _get_settings('5gs6lirpf6')
    cookie = _login(settings)
    if not cookie:
        return []
    try:
        from datetime import datetime, timedelta

        fmt = "%Y-%m-%d %H:%M:%S"
        dt_start = datetime.strptime(str(start_time)[:19], fmt) - timedelta(minutes=1)

        payload = {
            "format":    "json",
            "startTime": dt_start.strftime("%Y-%m-%dT%H:%M:%S"),
        }

        if end_time:
            dt_end = datetime.strptime(str(end_time)[:19], fmt) + timedelta(minutes=1)
            payload["endTime"] = dt_end.strftime("%Y-%m-%dT%H:%M:%S")

        resp = _request(settings, cookie, "cdrapi", payload)
        records = resp.get("cdr_root", [])
        print(f"[UCM CDR] {len(records)} records | {payload.get('startTime')} → {payload.get('endTime', 'open')}")
        return records

    finally:
        _logout(settings, cookie)