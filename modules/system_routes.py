from flask import jsonify


PALLET_PRO_CONSTITUTION = {
    "app": "Pallet Pro Core",
    "constitution_version": "0.1.0",
    "status": "active",
    "principle": "Pallet Pro must stay simple for users while preserving provable truth, auditability, and organisation boundaries.",
    "laws": [
        {
            "id": "LAW-001",
            "name": "Append-only transaction truth",
            "rule": "Posted transactions must not be edited or deleted directly. Corrections must be recorded as linked correction transactions.",
        },
        {
            "id": "LAW-002",
            "name": "Corrections, not rewrites",
            "rule": "Mistakes must be fixed with linked correction records, not silent edits.",
        },
        {
            "id": "LAW-003",
            "name": "Auditability always wins",
            "rule": "User simplicity must never weaken proof of who did what, when, where, and why.",
        },
        {
            "id": "LAW-004",
            "name": "Organisation separation",
            "rule": "One organisation must not access, alter, or infer private data belonging to another organisation except through approved shared-transaction workflows.",
        },
        {
            "id": "LAW-005",
            "name": "Role boundaries",
            "rule": "Org Admin, Global Admin, and Super Global Admin powers must remain separate and traceable.",
        },
        {
            "id": "LAW-006",
            "name": "External payments only",
            "rule": "Pallet Pro must not ask for, view, store, or process subscriber financial payment details. Third-party payment systems handle payments externally.",
        },
        {
            "id": "LAW-007",
            "name": "Pending instead of blocked",
            "rule": "Where safe and practical, blocked user actions should be saved as pending approval rather than discarded.",
        },
        {
            "id": "LAW-008",
            "name": "Field-first usability",
            "rule": "The app must remain fast, clear, and usable by field workers on mobile devices.",
        },
        {
            "id": "LAW-009",
            "name": "Law changes require owner approval",
            "rule": "Core constitution rules must not be changed casually or silently. Changes require explicit owner approval.",
        },
    ],
}


def root():
    return jsonify({
        "status": "Pallet Pro Core Running",
        "engine": "resources -> brands -> categories -> brand requests -> category requests -> resource requests -> partners -> transactions -> ledger -> stock -> audit -> pending approval -> depot profile -> admin dashboard",
    })


def register_system_routes(app):
    @app.get("/")
    @app.route("/health")
    @app.route("/api/health")
    def health():
        return jsonify({
            "status": "ok",
            "app": "Pallet Pro Core",
            "backend": "Flask",
            "port": 8000,
        })

    @app.route("/constitution")
    @app.route("/api/constitution")
    def get_constitution():
        return jsonify(PALLET_PRO_CONSTITUTION)

    @app.route("/api/system/status")
    def get_system_status():
        return jsonify({
            "status": "ok",
            "app": "Pallet Pro Core",
            "backend": "Flask",
            "port": 8000,
            "constitution": {
                "status": PALLET_PRO_CONSTITUTION["status"],
                "version": PALLET_PRO_CONSTITUTION["constitution_version"],
                "law_count": len(PALLET_PRO_CONSTITUTION["laws"]),
            },
            "modules": {
                "organisations": True,
                "partners": True,
                "partner_addresses": True,
                "depots": True,
                "resources": True,
                "transactions": True,
                "shared_transactions": True,
                "stock": True,
                "audit": True,
                "pending_approval": True,
                "qr_handoff": True,
            },
        })
