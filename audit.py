from db import make_id, now_iso


def audit_event(conn, entity_type, entity_id, action, summary, organisation_id=None):
    event_id = make_id("audit")
    conn.execute(
        """
        INSERT INTO audit_events (
            event_id,
            organisation_id,
            entity_type,
            entity_id,
            action,
            summary,
            created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            organisation_id,
            entity_type,
            entity_id,
            action,
            summary,
            now_iso(),
        ),
    )
