from __future__ import annotations

import sqlite3
from typing import List

from smart_gate.models.domain import EventRecord

MAX_SYNC_ATTEMPTS = 10
MAX_EVIDENCE_UPLOAD_ATTEMPTS = 5


class EventRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def add_event(self, event: EventRecord) -> None:
        self.conn.execute(
            """
            INSERT INTO event_queue (
                id, event_time, gate_id, lane_id, device_id, direction,
                plate_number_raw, plate_number_final, confidence,
                decision, decision_source,
                manual_by_user_id, manual_by_username,
                manual_reason_id, manual_reason, manual_note,
                is_offline_event, evidence_path,
                synced, sync_attempts, last_sync_error, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.id,
                event.event_time,
                event.gate_id,
                event.lane_id,
                event.device_id,
                event.direction,
                event.plate_number_raw,
                event.plate_number_final,
                event.confidence,
                event.decision,
                event.decision_source,
                event.manual_by_user_id,
                event.manual_by_username,
                event.manual_reason_id,
                event.manual_reason,
                event.manual_note,
                1 if event.is_offline_event else 0,
                event.evidence_path,
                1 if event.synced else 0,
                event.sync_attempts,
                event.last_sync_error,
                event.created_at,
            ),
        )
        self.conn.commit()

    def list_recent(self, limit: int = 20) -> List[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM event_queue ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()

    def list_unsynced(self, limit: int = 50) -> List[sqlite3.Row]:
        """Return unsynced events that have not exceeded the retry cap."""
        return self.conn.execute(
            """
            SELECT * FROM event_queue
            WHERE synced=0 AND sync_attempts < ?
            ORDER BY created_at ASC LIMIT ?
            """,
            (MAX_SYNC_ATTEMPTS, limit),
        ).fetchall()

    def mark_synced(self, event_id: str) -> None:
        self.conn.execute(
            "UPDATE event_queue SET synced=1, last_sync_error=NULL WHERE id=?",
            (event_id,),
        )
        self.conn.commit()

    def list_pending_evidence_upload(self, limit: int = 20) -> List[sqlite3.Row]:
        """
        Return synced events that have a local evidence file but whose
        evidence has not yet been successfully uploaded to the server.
        Skips events that have exceeded the retry cap.
        """
        return self.conn.execute(
            """
            SELECT * FROM event_queue
            WHERE synced = 1
              AND evidence_path IS NOT NULL
              AND evidence_path != ''
              AND (evidence_upload_status IS NULL OR evidence_upload_status = 'pending')
              AND (evidence_upload_attempts IS NULL OR evidence_upload_attempts < ?)
            ORDER BY created_at ASC LIMIT ?
            """,
            (MAX_EVIDENCE_UPLOAD_ATTEMPTS, limit),
        ).fetchall()

    def mark_evidence_uploaded(self, event_id: str, file_url: str) -> None:
        self.conn.execute(
            """
            UPDATE event_queue
            SET evidence_upload_status = 'uploaded',
                evidence_uploaded_url = ?
            WHERE id = ?
            """,
            (file_url, event_id),
        )
        self.conn.commit()

    def increment_evidence_upload_attempt(self, event_id: str, error: str) -> None:
        self.conn.execute(
            """
            UPDATE event_queue
            SET evidence_upload_status = 'pending',
                evidence_upload_attempts = COALESCE(evidence_upload_attempts, 0) + 1,
                last_sync_error = ?
            WHERE id = ?
            """,
            (f"evidence: {error}", event_id),
        )
        self.conn.commit()

    def increment_sync_attempt(self, event_id: str, error: str) -> None:
        self.conn.execute(
            """
            UPDATE event_queue
            SET sync_attempts = sync_attempts + 1, last_sync_error=?
            WHERE id=?
            """,
            (error, event_id),
        )
        self.conn.commit()
