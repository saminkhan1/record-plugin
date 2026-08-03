# Wexprolabs Record Privacy Policy

Effective date: July 27, 2026

This policy applies to the Record macOS application and the Record Chrome
Provider extension (together, “Record”). Wexprolabs provides Record to
organizations for use by their authorized employees and contractors.
The customer organization may also be a controller of employee data under its
agreement with Wexprolabs and its own workplace policies.

## Data Record handles

Record records only after an authorized user starts a workflow recording
and until it is stopped or reaches its configured maximum duration.

The Mac recorder may capture continuous video of visible content from the active
application, a time index for that video, mouse and keyboard input,
Accessibility application/window/control context, and non-secure text typed or
pasted during the demonstrated workflow. A paste may include up to 100,000
characters of clipboard text needed to reproduce that action. The Chrome
extension may capture active-page URLs and titles; browser, tab, window, frame,
and viewport context; clicks and interaction targets; non-password typed or
pasted text; selections, copies, and cuts; form choices; navigation; bounded
visible-tab screenshots; and upload or download metadata. Secure-input and
password-field values are redacted. Record does not read the contents of
uploaded or downloaded files.

Because an organization can record a workflow on a page containing business or
personal information, captured evidence may contain identifiers,
communications, financial or health information, authentication-related
information, location, browsing history, user activity, or website content.

## Where data goes

The Chrome extension sends recording data only through Chrome Native Messaging
to the Record application installed on the same Mac. Raw recording
bundles are stored in the selected local Record profile. For crash recovery, the
extension may temporarily keep a recording checkpoint and unsent action rows in
Chrome local extension storage.

Bounded evidence needed to draft a workflow may be sent to the model endpoint
configured by the customer organization. Workflow execution traces are sent to
the Langfuse endpoint configured by the customer organization. Those endpoints
may be local, self-hosted, or cloud-hosted. When an organization configures a
remote endpoint, the bounded draft input or trace data sent to that endpoint
leaves the Mac and is handled under that provider account and the
organization’s agreement with Wexprolabs. Record does not send a
separate copy to an advertising or unrelated analytics service.

## How data is used

Wexprolabs and Record use captured data only to:

- record the demonstrated workflow;
- create a workflow draft for review;
- run a reviewed workflow after required approvals;
- provide execution history, reliability, security, support, and debugging for
  that workflow; and
- comply with law or enforce the customer agreement.

Wexprolabs does not sell captured data, use it for advertising, use it to
determine creditworthiness or lending eligibility, or use it to train unrelated
general-purpose models. Wexprolabs does not transfer captured data except to
provide the product’s single purpose, at the customer’s direction, for security
or support under contract, or when legally required.

Use of information received from Google APIs complies with the Chrome Web Store
User Data Policy, including its Limited Use requirements.

## Retention and deletion

Local recording bundles, including dismissed recording evidence, workflow
drafts, installed jobs, and local execution records remain in the selected
Record profile until an authorized user or customer administrator permanently
deletes them or removes that profile. Dismissing a valid recording removes it
from active review without automatically deleting its recovery evidence.
Temporary Chrome checkpoints and acknowledged outbox rows are cleared when the
recording is finalized or dismissed.

Retention and deletion for a configured model or Langfuse endpoint are
controlled by the customer organization’s provider account and customer agreement.
Wexprolabs does not create an additional remote telemetry copy. A customer can
stop further remote processing by removing the configured endpoint credentials
and can request deletion of Wexprolabs-managed data through the support
channel below.

## Security and access

Record uses a local loopback dashboard, Chrome Native Messaging, scoped
local profiles, authenticated job APIs, protected-action approval checks, and
secret redaction. Access to customer data is limited to authorized customer
users and personnel who need access for contracted support, security, or legal
obligations.

## Contact

Wexprolabs customers should use their support contact. Product
and privacy requests may also be opened at:

https://github.com/saminkhan1/record-releases/issues

Do not include passwords, access tokens, or confidential recording contents in
a public issue.

## Changes

Wexprolabs will update this policy when Record’s data handling changes.
The effective date above identifies the current version.
