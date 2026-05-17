"""
Seed script — Pallet Pro help text library.

Run once (or re-run to update changed entries):
    python seed_help_text.py

Uses the db layer directly so the server does not need to be running.
Existing entries with matching context_key are updated in place.
New entries are inserted.
"""

import os
import sys

# Allow running from the repo root
sys.path.insert(0, os.path.dirname(__file__))

from db import get_conn, make_id, now_iso

ENTRIES = [

    # ------------------------------------------------------------------ #
    # Transactions                                                        #
    # ------------------------------------------------------------------ #
    {
        "context_key": "transaction.direction",
        "title": "IN or OUT?",
        "body": (
            "Choose IN when pallets or equipment are arriving at your depot — "
            "someone is dropping them off or returning them to you.\n\n"
            "Choose OUT when pallets or equipment are leaving your depot — "
            "you are sending them to a customer, partner, or another site."
        ),
    },
    {
        "context_key": "transaction.type",
        "title": "Transaction type",
        "body": (
            "The transaction type describes what kind of movement this is — "
            "for example a delivery, a collection, a transfer between sites, "
            "or a hire return.\n\n"
            "Your Org Admin sets up the types used by your organisation. "
            "If the right type is not in the list, ask your Org Admin to add it."
        ),
    },
    {
        "context_key": "transaction.partner",
        "title": "Who is the partner?",
        "body": (
            "The partner is the company or customer that the equipment is going "
            "to or coming from — the other party in this movement.\n\n"
            "If the partner is not in the list, select 'Partner not in system' "
            "at the bottom of the list and type their name. Your Org Admin will "
            "add them and complete the transaction."
        ),
    },
    {
        "context_key": "transaction.resource",
        "title": "What is the resource?",
        "body": (
            "The resource is the type of equipment being moved — for example "
            "a CHEP pallet, a plain timber pallet, a plastic pallet, or a skid.\n\n"
            "If the resource type is not in the list, select 'Resource not in system' "
            "at the bottom of the list and describe it. Your Org Admin will add it "
            "and complete the transaction."
        ),
    },
    {
        "context_key": "transaction.depot",
        "title": "Which depot?",
        "body": (
            "Select the depot or site where this movement is happening — "
            "the physical location where the equipment is being loaded or unloaded.\n\n"
            "Your Org Admin manages the list of depots. If your site is missing, "
            "contact your Org Admin."
        ),
    },
    {
        "context_key": "transaction.quantity",
        "title": "How many?",
        "body": (
            "Enter the number of items being moved. Count carefully — "
            "this is the number that gets recorded against your depot balance.\n\n"
            "If you are not sure of the exact count, enter your best estimate "
            "and add a note in the reason field. Your Org Admin can raise a "
            "correction if needed."
        ),
    },
    {
        "context_key": "transaction.missing_entity",
        "title": "Entity not in the system",
        "body": (
            "If you cannot find the partner, resource, or depot you need, "
            "do not leave the transaction incomplete.\n\n"
            "Select 'Not in system' from the bottom of the list, type a brief "
            "description of what is missing, and complete the rest of the form. "
            "Your transaction will be saved and sent to your Org Admin, who will "
            "add the missing item and confirm the transaction. You can move on."
        ),
    },
    {
        "context_key": "transaction.reference_number",
        "title": "Reference number",
        "body": (
            "The reference number is automatically assigned by Pallet Pro. "
            "It is a unique identifier for this transaction that you can quote "
            "if you need to look it up later or raise a query with your Org Admin."
        ),
    },
    {
        "context_key": "transaction.pending_approval",
        "title": "Why is this transaction pending?",
        "body": (
            "This transaction has been saved but not yet added to your depot's "
            "running balance. This usually means something needs to be set up "
            "first — for example an opening balance for the depot, or a missing "
            "partner or resource.\n\n"
            "Your Org Admin has been notified and will resolve it. No action is "
            "needed from you."
        ),
    },
    {
        "context_key": "transaction.correction",
        "title": "How to correct a posted transaction",
        "body": (
            "Once a transaction has been posted it cannot be edited — "
            "this keeps the records trustworthy.\n\n"
            "If you made a mistake, tap the transaction and select "
            "'Request Correction'. Describe what needs to change and submit. "
            "Your Org Admin will review it and apply the correction."
        ),
    },
    {
        "context_key": "transaction.note",
        "title": "Adding a note",
        "body": (
            "Use the note field to add any extra context — a docket number, "
            "a driver name, a vehicle registration, or anything else that helps "
            "identify this movement later.\n\n"
            "Notes are visible to your Org Admin and are part of the permanent record."
        ),
    },

    # ------------------------------------------------------------------ #
    # Resource Loss                                                       #
    # ------------------------------------------------------------------ #
    {
        "context_key": "resource_loss.what_is",
        "title": "What is a loss report?",
        "body": (
            "A loss report tells your Org Admin that some equipment has gone "
            "missing, been damaged, or been written off.\n\n"
            "Submitting a loss report does not immediately change your balance — "
            "your Org Admin reviews it first and then confirms or rejects it. "
            "This protects against accidental reports."
        ),
    },
    {
        "context_key": "resource_loss.loss_type",
        "title": "Loss type",
        "body": (
            "Choose the type that best describes what happened:\n\n"
            "DAMAGED — the equipment is still present but no longer usable.\n"
            "STOLEN — the equipment has been taken without authorisation.\n"
            "LOST — the equipment cannot be located.\n"
            "DESTROYED — the equipment has been intentionally disposed of.\n"
            "OTHER — use this if none of the above fit, and explain in the reason field."
        ),
    },
    {
        "context_key": "resource_loss.loss_reason",
        "title": "Reason for the loss",
        "body": (
            "Briefly describe what happened. The more detail you provide, "
            "the easier it is for your Org Admin to verify and confirm the report.\n\n"
            "Examples: 'Forklift drove through 6 pallets', "
            "'Loaded onto wrong truck — whereabouts unknown', "
            "'Found broken at back of yard, unsalvageable'."
        ),
    },
    {
        "context_key": "resource_loss.review_process",
        "title": "What happens after I submit?",
        "body": (
            "Your loss report is sent to your Org Admin for review.\n\n"
            "If they confirm it, the equipment count is reduced from your depot "
            "balance and a permanent record is created.\n\n"
            "If they reject it, the balance is not changed and they will provide "
            "a reason. You will be able to see the outcome in your report history."
        ),
    },

    # ------------------------------------------------------------------ #
    # Stocktake                                                           #
    # ------------------------------------------------------------------ #
    {
        "context_key": "stocktake.what_is",
        "title": "What is a stocktake?",
        "body": (
            "A stocktake is a physical count of all equipment at your depot. "
            "You go through each resource type, count what is actually there, "
            "and enter the number.\n\n"
            "Pallet Pro then compares your count to the expected balance and "
            "highlights any differences so your Org Admin can investigate."
        ),
    },
    {
        "context_key": "stocktake.quantity_counted",
        "title": "Enter your counted quantity",
        "body": (
            "Enter the number you physically counted for this item — "
            "what you can actually see and touch right now, not what the system says.\n\n"
            "If a resource type has none present, enter 0. "
            "Do not skip lines — enter a count for every item shown."
        ),
    },
    {
        "context_key": "stocktake.variance",
        "title": "What does variance mean?",
        "body": (
            "Variance is the difference between what the system expected and "
            "what you actually counted.\n\n"
            "A positive variance means you counted more than expected. "
            "A negative variance means you counted fewer.\n\n"
            "Your Org Admin will review any variances and decide whether to "
            "accept or reject each line."
        ),
    },
    {
        "context_key": "stocktake.accept_reject",
        "title": "Accepting and rejecting lines",
        "body": (
            "Accept a line when you are satisfied the count is correct and want "
            "the system balance updated to match.\n\n"
            "Reject a line when you believe the count was wrong or needs to be "
            "recounted. A rejected line stays at the current system balance — "
            "nothing changes until it is resubmitted and accepted."
        ),
    },

    # ------------------------------------------------------------------ #
    # Depots                                                              #
    # ------------------------------------------------------------------ #
    {
        "context_key": "depot.what_is",
        "title": "What is a depot?",
        "body": (
            "A depot is a physical location where equipment is stored and moved. "
            "It could be a warehouse, a yard, a distribution centre, or any site "
            "that your organisation uses to track stock.\n\n"
            "Every transaction is recorded against a specific depot so you always "
            "know the balance at each location."
        ),
    },
    {
        "context_key": "depot.opening_balance",
        "title": "Opening balance",
        "body": (
            "The opening balance is how many items were at this depot when "
            "tracking started in Pallet Pro. It is the starting point for all "
            "future transactions.\n\n"
            "Only your Org Admin can set the opening balance. Until it is set, "
            "transactions at this depot will be held in a pending queue and "
            "posted automatically once the balance is confirmed."
        ),
    },
    {
        "context_key": "depot.deactivate",
        "title": "Deactivating a depot",
        "body": (
            "Deactivating a depot means it no longer appears in transaction "
            "drop-down lists. All history is preserved — nothing is deleted.\n\n"
            "If a depot is closed or no longer in use, deactivating it keeps "
            "your lists clean. It can be reactivated at any time."
        ),
    },

    # ------------------------------------------------------------------ #
    # Resources                                                           #
    # ------------------------------------------------------------------ #
    {
        "context_key": "resource.what_is",
        "title": "What is a resource?",
        "body": (
            "A resource is a type of Transport Handling Equipment — the physical "
            "items that Pallet Pro tracks. Examples: CHEP pallets, plain timber pallets, "
            "plastic pallets, half pallets, skids, crates.\n\n"
            "Each resource type has its own running balance at each depot."
        ),
    },
    {
        "context_key": "resource.unit_type",
        "title": "Unit type",
        "body": (
            "The unit type describes how this resource is counted — for example "
            "EACH (individual items), BUNDLE (groups), or STACK.\n\n"
            "Most resources are counted as EACH. Your Org Admin sets this when "
            "the resource is created."
        ),
    },

    # ------------------------------------------------------------------ #
    # Partners                                                            #
    # ------------------------------------------------------------------ #
    {
        "context_key": "partner.what_is",
        "title": "What is a partner?",
        "body": (
            "A partner is a company or customer that you regularly exchange "
            "equipment with — a customer you deliver pallets to, a supplier "
            "you receive pallets from, or another site in your network.\n\n"
            "Linking a partner to a transaction tells you not just how many "
            "items moved, but who they went to or came from."
        ),
    },
    {
        "context_key": "partner.site",
        "title": "Partner site",
        "body": (
            "Some partners have multiple sites or delivery addresses. "
            "The site field lets you specify exactly which location "
            "within that partner the movement relates to.\n\n"
            "If the partner has only one address, this field may be "
            "pre-filled automatically."
        ),
    },

    # ------------------------------------------------------------------ #
    # Offline mode                                                        #
    # ------------------------------------------------------------------ #
    {
        "context_key": "offline.queue",
        "title": "Working offline",
        "body": (
            "You can enter transactions even when there is no signal. "
            "Pallet Pro saves them to a queue on your device.\n\n"
            "When you are back in range and open the app, the queue uploads "
            "automatically. Your transactions are timestamped with the time "
            "you entered them — not the upload time."
        ),
    },
    {
        "context_key": "offline.sync",
        "title": "Syncing your offline queue",
        "body": (
            "Pallet Pro uploads your queued transactions automatically when "
            "you open the app and a connection is available.\n\n"
            "You will see a summary of what was sent and whether any items "
            "need attention. If an item fails, it stays in the queue and "
            "you will be notified."
        ),
    },

    # ------------------------------------------------------------------ #
    # Auth / Login                                                        #
    # ------------------------------------------------------------------ #
    {
        "context_key": "auth.passkey",
        "title": "What is a passkey?",
        "body": (
            "A passkey lets you log in using your fingerprint, face, or "
            "device PIN — no password to type.\n\n"
            "It is faster and more secure than a password. Once you set it up, "
            "tap the fingerprint or face icon on the login screen to get in instantly."
        ),
    },
    {
        "context_key": "auth.lock",
        "title": "Lock vs log out",
        "body": (
            "Locking the app keeps you logged in but requires your biometric or PIN "
            "to get back in — like locking your phone screen. "
            "Use this during breaks or when handing the device to someone else.\n\n"
            "Logging out removes your session completely and requires your full "
            "credentials to log back in. You do not need to log out between shifts — "
            "just lock."
        ),
    },

    # ------------------------------------------------------------------ #
    # Transaction Correction Requests                                     #
    # ------------------------------------------------------------------ #
    {
        "context_key": "tcr.what_is",
        "title": "What is a correction request?",
        "body": (
            "A correction request lets you flag a posted transaction that "
            "contains an error. Because Pallet Pro never deletes or edits "
            "records, corrections are applied by adding a new linked transaction "
            "that fixes the mistake — the original is preserved.\n\n"
            "Your Org Admin reviews the request and applies the correction."
        ),
    },
    {
        "context_key": "tcr.when_to_use",
        "title": "When should I raise a correction?",
        "body": (
            "Raise a correction when:\n\n"
            "— You entered the wrong quantity\n"
            "— You selected the wrong resource type\n"
            "— The transaction was posted against the wrong partner or depot\n"
            "— The direction was wrong (IN instead of OUT or vice versa)\n\n"
            "If you just want to add a note without changing numbers, "
            "use the transaction note field instead."
        ),
    },
    {
        "context_key": "tcr.append_only",
        "title": "Why can't I just edit the transaction?",
        "body": (
            "Pallet Pro is built on provable truth — every record is permanent "
            "and nothing is silently changed. This means you, your Org Admin, "
            "and your partners can always trust the history.\n\n"
            "Corrections create a transparent trail: the original transaction, "
            "the correction request, and the fix are all visible."
        ),
    },

    # ------------------------------------------------------------------ #
    # Shared Transactions                                                 #
    # ------------------------------------------------------------------ #
    {
        "context_key": "shared_transaction.what_is",
        "title": "What is a shared transaction?",
        "body": (
            "A shared transaction records a movement that involves two "
            "organisations — for example when you deliver pallets to a customer "
            "who also uses Pallet Pro.\n\n"
            "Both sides of the exchange are linked, so any dispute about "
            "quantities can be resolved with a clear record of what was agreed."
        ),
    },
    {
        "context_key": "shared_transaction.qr_handoff",
        "title": "QR handoff",
        "body": (
            "A QR handoff lets both parties confirm a shared transaction on site. "
            "The sender generates a QR code, the receiver scans it to confirm "
            "receipt, and both records are updated simultaneously.\n\n"
            "This eliminates disputes about whether equipment was delivered."
        ),
    },

    # ------------------------------------------------------------------ #
    # Referral                                                            #
    # ------------------------------------------------------------------ #
    {
        "context_key": "referral.recommend",
        "title": "Recommend Pallet Pro",
        "body": (
            "Tap this button to generate a personal referral link. "
            "Share it with another company or contact — when they scan "
            "your QR code or follow your link and install Pallet Pro, "
            "the referral is tracked back to you.\n\n"
            "Your Org Admin can see how many installs have come from your referrals."
        ),
    },

    # ------------------------------------------------------------------ #
    # Branding                                                            #
    # ------------------------------------------------------------------ #
    {
        "context_key": "branding.logo",
        "title": "Organisation logo",
        "body": (
            "Upload your company logo here. It will appear at the top of "
            "the Pallet Pro dashboard for your organisation.\n\n"
            "Accepted formats: PNG, JPEG, WebP. Maximum file size: 2 MB. "
            "A square or horizontal logo works best."
        ),
    },
    {
        "context_key": "branding.primary_colour",
        "title": "Brand colour",
        "body": (
            "Set your company's primary colour and the Pallet Pro dashboard "
            "will use it for buttons, highlights, and accents.\n\n"
            "Enter a hex colour code — for example #FF6600 for orange or "
            "#003366 for navy. Ask your marketing team if you are unsure of "
            "your brand colour code."
        ),
    },

    # ------------------------------------------------------------------ #
    # Subscription / Users                                                #
    # ------------------------------------------------------------------ #
    {
        "context_key": "subscription.user_count",
        "title": "Number of users",
        "body": (
            "This is the number of Pallet Pro user accounts your organisation "
            "is subscribed to — including your Org Admin account.\n\n"
            "If you need more users than your current subscription allows, "
            "update your user count here. Organisations with more than 75 users "
            "should contact Pallet Pro for a custom plan."
        ),
    },
    {
        "context_key": "subscription.temp_user",
        "title": "Temporary user access",
        "body": (
            "Temporary users get 28 days of access — useful for contractors, "
            "seasonal workers, or covering a colleague on leave.\n\n"
            "When the 28 days are up, their access is automatically suspended. "
            "No action is needed from you. Temporary users are billed separately "
            "from your main subscription."
        ),
    },

    # ------------------------------------------------------------------ #
    # General / Core concepts                                             #
    # ------------------------------------------------------------------ #
    {
        "context_key": "general.audit_trail",
        "title": "Audit trail",
        "body": (
            "Every action in Pallet Pro — every transaction, every correction, "
            "every status change — is permanently recorded with a timestamp and "
            "the name of the person who made it.\n\n"
            "Nothing is ever silently changed or deleted. This gives you a "
            "complete, trustworthy history that you can refer to at any time."
        ),
    },
    {
        "context_key": "general.pending_review",
        "title": "What does 'pending review' mean?",
        "body": (
            "Pending review means a record has been submitted and is waiting "
            "for your Org Admin to check and confirm it.\n\n"
            "The record is saved — nothing will be lost. Your Org Admin will "
            "be notified and will either confirm it (which posts it to the balance) "
            "or come back to you with questions."
        ),
    },
    {
        "context_key": "general.org_admin",
        "title": "Who is the Org Admin?",
        "body": (
            "The Org Admin is the person responsible for managing your "
            "organisation's Pallet Pro account — adding users, setting up "
            "depots and resources, reviewing pending transactions, and "
            "confirming loss reports.\n\n"
            "If you are unsure who your Org Admin is, ask your supervisor."
        ),
    },
    {
        "context_key": "general.balance",
        "title": "What is the depot balance?",
        "body": (
            "The depot balance is the running count of equipment at a "
            "specific location. It increases when items come IN and "
            "decreases when items go OUT.\n\n"
            "The balance is calculated from all posted transactions. "
            "Pending or draft transactions do not affect it until they are posted."
        ),
    },
    {
        "context_key": "general.draft",
        "title": "What is a draft transaction?",
        "body": (
            "A draft transaction has been created but not yet posted to the "
            "balance. It is saved and visible but the equipment count has not "
            "changed yet.\n\n"
            "Post the transaction when you are ready to confirm the movement "
            "and update the balance."
        ),
    },
]


def seed(db_path=None):
    if db_path:
        os.environ["PALLET_PRO_DB"] = db_path

    conn = get_conn()
    conn.execute("""
    CREATE TABLE IF NOT EXISTS help_texts (
        help_text_id            TEXT PRIMARY KEY,
        context_key             TEXT UNIQUE NOT NULL,
        title                   TEXT NOT NULL,
        body                    TEXT NOT NULL,
        is_active               INTEGER NOT NULL DEFAULT 1,
        created_by_display_name TEXT NOT NULL,
        updated_by_display_name TEXT,
        created_at              TEXT NOT NULL,
        updated_at              TEXT NOT NULL
    )
    """)
    conn.commit()

    inserted = 0
    updated = 0

    for entry in ENTRIES:
        ts = now_iso()
        existing = conn.execute(
            "SELECT help_text_id FROM help_texts WHERE context_key = ?",
            (entry["context_key"],),
        ).fetchone()

        if existing:
            conn.execute(
                """UPDATE help_texts
                   SET title = ?, body = ?, is_active = 1,
                       updated_by_display_name = 'AI seed', updated_at = ?
                   WHERE context_key = ?""",
                (entry["title"], entry["body"], ts, entry["context_key"]),
            )
            updated += 1
        else:
            conn.execute(
                """INSERT INTO help_texts
                   (help_text_id, context_key, title, body, is_active,
                    created_by_display_name, created_at, updated_at)
                   VALUES (?, ?, ?, ?, 1, 'AI seed', ?, ?)""",
                (make_id("help"), entry["context_key"],
                 entry["title"], entry["body"], ts, ts),
            )
            inserted += 1

    conn.commit()
    conn.close()

    print(f"Help text seeded: {inserted} inserted, {updated} updated.")
    print(f"Total entries: {len(ENTRIES)}")


if __name__ == "__main__":
    db_arg = sys.argv[1] if len(sys.argv) > 1 else None
    seed(db_arg)
