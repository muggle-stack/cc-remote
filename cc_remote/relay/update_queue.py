"""Device-scoped update intents and last verified maintenance status."""
from __future__ import annotations

import json
import time

from cc_remote.update import UpdateError, version_tuple
from cc_remote.update_jobs import TERMINAL, job_id, public_job


class DeviceUpdateQueue:
    def __init__(self, devices):
        self.devices = devices
        with devices._connect() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS device_update_status "
                               "(machine TEXT PRIMARY KEY, value TEXT NOT NULL)")
            connection.execute("CREATE TABLE IF NOT EXISTS device_update_jobs "
                               "(machine TEXT NOT NULL, id TEXT NOT NULL, value TEXT NOT NULL, "
                               "PRIMARY KEY(machine, id))")

    def status(self, machine: str) -> dict | None:
        with self.devices._connect() as connection:
            row = connection.execute("SELECT value FROM device_update_status WHERE machine=?", (machine,)).fetchone()
            return json.loads(row[0]) if row else None

    def remember(self, machine: str, value: dict) -> None:
        if value.get("schema") != 1 or not isinstance(value.get("current"), str):
            return
        version_tuple(value["current"])
        # Only the bounded public maintenance projection is stored by Relay.
        fields = {key: value[key] for key in ("schema", "managed", "enabled", "reason", "current",
                                             "allow_relay_update", "latest", "busy_count") if key in value}
        raw = json.dumps(fields)
        if len(raw) > 16384:
            raise UpdateError("oversized device update status")
        with self.devices._connect() as connection:
            connection.execute("INSERT INTO device_update_status(machine,value) VALUES (?,?) "
                               "ON CONFLICT(machine) DO UPDATE SET value=excluded.value", (machine, raw))

    def job(self, machine: str, identity: str) -> dict | None:
        job_id(identity)
        with self.devices._connect() as connection:
            row = connection.execute("SELECT value FROM device_update_jobs WHERE machine=? AND id=?",
                                     (machine, identity)).fetchone()
            return json.loads(row[0]) if row else None

    def active(self, machine: str) -> dict | None:
        with self.devices._connect() as connection:
            row = connection.execute("SELECT value FROM device_update_jobs WHERE machine=? ORDER BY rowid DESC LIMIT 1",
                                     (machine,)).fetchone()
            return json.loads(row[0]) if row else None

    def save(self, machine: str, value: dict) -> None:
        job_id(value["id"])
        with self.devices._connect() as connection:
            connection.execute("UPDATE device_update_jobs SET value=? WHERE machine=? AND id=?",
                               (json.dumps(value), machine, value["id"]))

    def create(self, machine: str, identity: str, version: str) -> dict:
        job_id(identity)
        version_tuple(version)
        with self.devices._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT value FROM device_update_jobs WHERE machine=? AND id=?",
                                     (machine, identity)).fetchone()
            if row:
                value = json.loads(row[0])
                if value["version"] != version:
                    raise UpdateError("update request identity was reused")
                return value
            row = connection.execute("SELECT value FROM device_update_jobs WHERE machine=? ORDER BY rowid DESC LIMIT 1",
                                     (machine,)).fetchone()
            if row and json.loads(row[0])["phase"] not in TERMINAL:
                raise UpdateError("another update is already pending")
            row = connection.execute("SELECT value FROM device_update_status WHERE machine=?", (machine,)).fetchone()
            known = json.loads(row[0]) if row else {}
            if known.get("managed") is not True or known.get("enabled") is not True:
                raise UpdateError("device UI updates are disabled or have not been verified")
            if version_tuple(version) <= version_tuple(known["current"]):
                raise UpdateError("selected update is not newer")
            count = connection.execute("SELECT COUNT(*) FROM device_update_jobs WHERE machine=?", (machine,)).fetchone()[0]
            if count >= 1024:
                raise UpdateError("update receipt limit reached; administrator maintenance required")
            value = {"id": identity, "version": version, "previous": known["current"],
                     "phase": "waiting_device", "reason": None, "created_at": time.time(),
                     "updated_at": time.time(), "delivered": False}
            connection.execute("INSERT INTO device_update_jobs(machine,id,value) VALUES (?,?,?)",
                               (machine, identity, json.dumps(value)))
            return value

    def observe(self, machine: str, value: dict) -> None:
        identity = value.get("id")
        if not isinstance(identity, str):
            return
        prior = self.job(machine, identity)
        if prior is None or prior["version"] != value.get("version"):
            return
        self.save(machine, {**prior, **public_job(value), "delivered": True})

    def cancel(self, machine: str, identity: str) -> dict | None:
        value = self.job(machine, identity)
        if value is not None and value["phase"] not in TERMINAL:
            value = {**value, "cancel_requested": True, "updated_at": time.time()}
            if not value["delivered"]:
                value.update(phase="cancelled", reason=None)
            self.save(machine, value)
        return value
