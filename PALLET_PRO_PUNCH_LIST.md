
## Product workflow gaps / floating-feature watchlist

These items were previously checked during a system-wide scan for small created functions or loose features. The scan returned nil, but these concepts are now confirmed as required punch-list items and must not be forgotten.

### Known items to add

- [ ] **Unsubscribe button**
  - Confirm where unsubscribe belongs: subscription, notification emails, marketing-style emails, in-app notices, or all relevant places.
  - Keep payment/subscription handling external where required.
  - Do not collect or store card/bank/payment details inside Pallet Pro.

- [ ] **Help function**
  - Add a clear help/support function for users.
  - Prefer context-aware help based on the current module/page.
  - Keep wording practical and role-safe.

- [ ] **Reporting module**
  - Continue building reporting as a proper first-class module.
  - Backend remains source of truth for report data.
  - Avoid frontend-only calculations pretending to be business truth.
  - Include permissions, org boundaries, exports, and dashboard summaries.

- [ ] **Context relationship open buttons**
  - When a user is inside a module, show safe open/view buttons for related records.
  - Example relationship targets may include depots, partners, resources, users, transactions, stocktakes, losses, TCRs, reports, and related tables.
  - Buttons must respect role permissions and org boundaries.
  - Do not expose records just because they are related.
  - This should make Pallet Pro feel GPS-clear: users can see where they are, what they are dealing with, and what related thing they can open next.

### Notes

- More items are expected.
- Add future remembered items to this section or a later punch-list batch.
- Treat these as required product/workflow gaps until inspected, designed, implemented, tested, and closed.

## Additional product/system punch-list batch

### Backup, monitoring, access, admin, and management modules

- [ ] **Backup process**
  - Define full backup process for Pallet Pro data.
  - Include in-house security protocols.
  - Include backup frequency, storage location, verification, restore testing, access control, approval steps, and audit logging.
  - Restore must be heavily guarded and never casual-click dangerous.

- [ ] **Monitoring module for visual displays**
  - Create a monitoring module for visual dashboards/displays.
  - Record the source data used to create each visual output.
  - Keep visual outputs traceable back to backend truth.
  - Avoid frontend-only “pretty numbers” without backend source records.

- [ ] **Show Password button on login screen**
  - Add a Show Password button to the login password field.
  - Button should reveal the password as it is typed or reveal what has already been typed.
  - Keep behavior simple, visible, and user-controlled.

- [ ] **Cloud hosting previous to-do items**
  - Recover the 3 to 4 cloud-hosting related items from the previous to-do list.
  - Confirm AWS / third-party hosting responsibilities.
  - Keep Docker out of the customer-facing product route.
  - Keep Pallet Pro as HTTP / PWA / web portal architecture.
  - Document hot/warm server expectations and hosting handoff boundaries.

- [ ] **Subscription page including pricing table**
  - Add subscription page.
  - Include pricing table.
  - Keep payment handling external through third-party paywall.
  - Do not store card, bank, or payment details inside Pallet Pro.

- [ ] **QR code transaction sharing between Pallet Pro orgs**
  - Design QR-based transaction sharing between authorised Pallet Pro organisations.
  - Must respect org boundaries, permissions, audit trail, and explicit user action.
  - No silent cross-org data leakage.

- [ ] **QR code for referring Pallet Pro**
  - Add QR code referral flow for Pallet Pro.
  - Track referral intent safely.
  - Keep referral separate from transaction/business records unless explicitly linked.

- [ ] **User Management**
  - Review and complete user management flows.
  - Include invite, activate, deactivate, role changes, org assignment, and audit trail.
  - Ensure frontend and backend permissions match.

- [ ] **Customer/Supplier Management**
  - Review and complete customer/supplier management.
  - Clarify partner/customer/supplier naming and relationship model.
  - Ensure address, contact, transaction, and reporting links are wired.

- [ ] **Address Management**
  - Review and complete address management.
  - Include partner addresses, depot addresses, route intelligence use, QR/link use, and audit-safe updates.
  - Ensure inactive addresses are not used by new workflows unless intentionally reactivated.

- [ ] **Audit Trail Management**
  - Review and complete audit trail management.
  - Ensure important business actions create useful audit records.
  - Include who, what, when, where relevant, before/after values, and org context.
  - Keep audit records read-only from normal workflows.

- [ ] **Stocktake Management**
  - Review and complete stocktake management.
  - Include initiation, line counting, submit/review, discrepancy handling, approval/rejection, reporting, and audit trail.
  - Ensure stocktake workflows do not mutate ledger/stock incorrectly.

## Super Boss live app review findings

### Dashboard

- [ ] Top dashboard metric cards are display-only.
  - Each card must be clickable.
  - Each card must drill down to the records/details that produced the number.
  - User must be able to answer: "Where did this number come from?"

- [ ] System Console metrics are display-only.
  - Each metric needs a details link or drill-down.
  - Visual outputs must connect back to source records, logs, queues, orgs, users, or audit data.
  - No mystery numbers.

### All Organisations

- [ ] All Organisations page shows "Unable to load Org."
  - Inspect frontend/backend route mismatch.
  - Confirm Super Global Admin can list and open organisations.

- [ ] Opening an organisation appears to require two clicks.
  - Review UX flow.
  - One intentional click should open the organisation unless there is a clear confirm step.

- [ ] Organisation listing needs clearer ordering/filtering.
  - Add supplier/customer/all partner style filtering where applicable.
  - Add clear default order.
  - User must understand what order the orgs are listed in.

### Pricing

- [ ] Complete pricing table is missing.
- [ ] Update pricing function is missing.
- [ ] Text before pricing table is missing.
- [ ] Text after pricing table is missing.
- [ ] Pricing conditions/explanatory terms are missing.
- [ ] Keep actual payment processing external.
- [ ] Do not store card, bank, or payment details inside Pallet Pro.

### Temporary users

- [ ] Conditions applying to temporary users are missing.
  - Explain what temporary users can and cannot do.
  - Explain expiry, access limits, org boundary, and audit behavior.

### Login Integrity

- [ ] Login Integrity page is display-only.
  - Add active links/buttons to reports, events, sessions, users, actions, and source records.
  - Every displayed count/status must have a "show me the details" path.

### User Access

- [ ] User Access page purpose is unclear.
  - Rename, explain, or split the page if needed.
  - Add plain-English description of what the user is looking at.
  - Add active links/buttons to related users, sessions, temporary access, login integrity, roles, and audit records.
  - Avoid admin screens that are just static output panels.

### Product law reinforced

- [ ] Pallet Pro dashboards must not be dead displays.
- [ ] Every metric should have a traceable source.
- [ ] Every admin screen should answer: what is this, why does it matter, and what can I open or do next?
- [ ] Keep UX GPS-clear: where am I, what am I looking at, and where can I go from here?
