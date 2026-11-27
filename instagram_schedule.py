"""Queue approved La Frise carousels for the following day in Paris."""
import argparse
import json
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from instagram_publish import PublishError, graph_call, publish, validate_job

PARIS = ZoneInfo("Europe/Paris")


def connect(database):
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database, timeout=15)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS instagram_schedule (
            publish_at TEXT PRIMARY KEY,
            subject_id TEXT UNIQUE NOT NULL,
            manifest TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'queued',
            outcome TEXT
        )
    """)
    return connection


def put(database, manifest, hour, now):
    if hour not in (7, 17):
        raise PublishError("Créneaux disponibles : 7 h et 17 h, heure de Paris.")
    if not isinstance(manifest, dict):
        raise PublishError("Manifeste invalide.")
    validate_job(manifest)
    if manifest.get("texts_approved") is not True or manifest.get("visuals_approved") is not True:
        raise PublishError("Le texte et les cartes doivent être validés.")
    if re.search(r"https?://|www\.", manifest.get("caption", ""), re.I):
        raise PublishError("La légende contient une URL.")
    subject = manifest.get("subject_id")
    if not isinstance(subject, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", subject):
        raise PublishError("Identifiant de sujet invalide.")
    approved = datetime.fromisoformat(manifest["visuals_approved_at"])
    if approved.tzinfo is None or approved > now:
        raise PublishError("Horodatage de validation visuelle invalide.")
    day = approved.astimezone(PARIS).date() + timedelta(days=1)
    due = datetime.combine(day, time(hour), tzinfo=PARIS).astimezone(timezone.utc)
    if due <= now:
        raise PublishError("Le créneau du lendemain est déjà passé.")
    payload = json.dumps(manifest, ensure_ascii=False, sort_keys=True)
    with closing(connect(database)) as db, db:
        try:
            db.execute("INSERT INTO instagram_schedule (publish_at, subject_id, manifest) VALUES (?, ?, ?)",
                       (due.isoformat(), subject, payload))
        except sqlite3.IntegrityError:
            row = db.execute(
                "SELECT publish_at, manifest FROM instagram_schedule WHERE subject_id=?", (subject,)
            ).fetchone()
            if row != (due.isoformat(), payload):
                raise PublishError("Le sujet ou le créneau est déjà réservé.") from None
    return due.astimezone(PARIS).isoformat()


def run_due(database, now, publish_job):
    minute = now.astimezone(timezone.utc).replace(second=0, microsecond=0).isoformat()
    with closing(connect(database)) as db:
        with db:
            db.execute(
                "UPDATE instagram_schedule SET state='missed' WHERE state='queued' AND publish_at < ?",
                (minute,),
            )
            claim = db.execute(
                "UPDATE instagram_schedule SET state='publishing' "
                "WHERE publish_at=? AND state='queued' RETURNING subject_id, manifest",
                (minute,),
            ).fetchone()
        if claim is None:
            return None
        subject, manifest = claim
        try:
            outcome = publish_job(subject, json.loads(manifest))
            state = "archive_error" if "archive_error" in outcome else "published"
        except (PublishError, OSError, ValueError) as exc:
            outcome = {"error": str(exc)}
            # A provider timeout may mean the post already exists.
            state = "verification_required"
        with db:
            db.execute("UPDATE instagram_schedule SET state=?, outcome=? WHERE publish_at=?",
                       (state, json.dumps(outcome, ensure_ascii=False), minute))
        return {"subject_id": subject, "state": state, **outcome}


def publish_on_render(subject, job, directory):
    token = os.environ.get("INSTAGRAM_ACCESS_TOKEN", "")
    account = os.environ.get("INSTAGRAM_ACCOUNT_ID", "")
    version = os.environ.get("INSTAGRAM_API_VERSION", "")
    if not token or not re.fullmatch(r"[0-9]+", account) or not re.fullmatch(r"v[0-9]+\.[0-9]+", version):
        raise PublishError("Configuration Instagram incomplète.")

    def call(path, **kwargs):
        return graph_call(version, token, path, host="graph.facebook.com", **kwargs)

    if call(account, params={"fields": "username"}).get("username") != "lafrise.fr":
        raise PublishError("Le compte renvoyé ne correspond pas à @lafrise.fr.")
    return publish(job, account, call, directory / f"{subject}.receipt",
                   archive_dir=directory / "Carrousels_publies")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--enqueue", type=Path)
    action.add_argument("--run-due", action="store_true")
    parser.add_argument("--hour", type=int, choices=(7, 17))
    args = parser.parse_args()
    now = datetime.now(timezone.utc)
    try:
        if args.enqueue:
            job = json.loads(args.enqueue.read_text(encoding="utf-8"))
            due = put(args.db, job, args.hour, now)
            print(json.dumps({"publish_at": due}))
            return 0
        if args.hour is not None:
            parser.error("--hour s'utilise avec --enqueue.")
        outcome = run_due(args.db, now, lambda subject, job: publish_on_render(subject, job, args.db.parent))
        if outcome is not None:
            print(json.dumps(outcome, ensure_ascii=False))
            return int(outcome["state"] != "published")
        return 0
    except (KeyError, TypeError, ValueError, OSError, PublishError, sqlite3.Error) as exc:
        parser.exit(1, f"Planification interrompue. {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
