# Record Chrome Provider submission

This is the reviewed submission inventory for extension version `0.2.2`. It is
an operator checklist, not authorization to create or publish a Chrome Web Store
item.

## Account and identity gate

- [x] Verify the paid publisher account. On 2026-07-23 the active publisher
      `saminbbkhan` accepted the first draft item.
- [x] The assigned Item ID and dashboard public key are bound in the manifest and
      `store-identity.json`; the packaging gate derives and verifies the same ID.
- [x] Run `npm test` and `npm run package:chrome`, then upload
      `dist/chrome/hermes-chrome-provider-0.2.2.zip` to the existing draft item.
      Do not create a replacement item.
- [x] Compare `dist/chrome/package-receipt.json` with the reviewed source and
      commit before building the signed Mac release.

The assigned Chrome Web Store Item ID is
`anaccpcogeinfllkciaheolngknaabbb`. The retained bootstrap audit identity is
`pjgpgncokdchfamgpaniekieplapoacg`. Never create a replacement item to change
either identity, and never publish from this checklist.

## Store listing template

Name: `Record Chrome Provider`

Description opening: `Record Chrome Provider works with the Record Mac application.`

Homepage URL: `https://wexprolabs.com/workflow-automation/`

Category: `Workflow & Planning`

Language: `English (United States)`

Detailed description:

> Record Chrome Provider works with the Record Mac application. Its single
> purpose is to capture a user's Chrome actions and visual evidence only while a
> local Record recording is active, so Record can create a workflow draft
> for the user to review.
>
> During an active recording, the extension captures active-page URLs and
> titles; tab, window, frame, browser, and viewport context; interaction targets,
> labels, selectors, and coordinates; non-password typed or pasted text;
> selected, copied, and cut text; form selections; upload and download metadata;
> navigation; and visible-tab screenshots after meaningful actions.
> Password-field values are redacted. It does not read uploaded or downloaded
> file contents. Chrome-restricted pages remain inaccessible.
>
> For crash recovery, the extension may temporarily store a recording checkpoint
> and unsent action rows in Chrome's local extension storage. It transfers
> recording data through Chrome Native Messaging to the Record native application
> installed on this Mac. Recording bundles are stored in the selected local
> Record profile. Bounded recording evidence may be processed by the user's
> configured model to create the draft, and execution traces are exported to the
> user's configured Langfuse service, as described in the product privacy policy.
>
> The extension has no standalone function without the local Record Mac
> application.

Single purpose:

> Capture a user's Chrome actions and visual evidence only while a local Record
> recording is active, so Record can create a workflow draft for the user
> to review.

Prominent data disclosure:

> During an active recording, the extension captures visited page URLs and
> titles, interaction targets and coordinates, non-password typed or pasted
> text, selected or copied text, upload and download metadata, browser/frame
> context, and visible-tab screenshots. Password-field values are redacted.
> The extension may temporarily retain a recording checkpoint and unsent action
> rows in Chrome's local extension storage for crash recovery. It sends
> recording data only to the Record native application installed on this Mac.

Do not add claims about retention, encryption, support, pricing, availability,
or remote data handling unless product/legal has supplied and verified them.

## Permission justifications

- `tabs`: identify the active tab and record its URL, title, tab/window identity,
  lifecycle, and visible-tab screenshots during an active recording.
- `webNavigation`: record top-frame history and single-page navigation changes
  needed to reproduce the workflow.
- `scripting`: attach the reviewed recorder scripts to eligible already-open
  pages when a recording starts; manifest content scripts cover new pages.
- `downloads`: record download state and metadata such as URL, filename, MIME
  type, size, and errors as workflow effects. It does not read downloaded file
  contents.
- `alarms`: wake the Manifest V3 service worker to renew the local recording
  segment and detect continuity loss.
- `storage`: retain the active recording checkpoint and an acknowledged action
  outbox across service-worker suspension or native-host restart.
- `nativeMessaging`: transfer the active recording to the bundled local native
  host `com.hermes.chrome_capture`.
- Host access `<all_urls>`: workflows may cross arbitrary customer-selected
  sites and frames. Local `file://` pages require the user to enable Chrome's
  separate **Allow access to file URLs** setting. Capture is persisted only
  while Record reports an active recording; Chrome-restricted pages remain
  inaccessible.

The extension source has no direct HTTP, WebSocket, or analytics client. Its
transfer boundary is `chrome.runtime.connectNative("com.hermes.chrome_capture")`;
the native Record product owns any later storage or processing. The extension
listing describes that local extension-to-native boundary; the product privacy
policy separately describes configured-model drafting and configured-Langfuse
trace export.

Select **No, I am not using Remote code**. Every executable extension source
file is contained in the submitted package; it has no remote script/module
reference, remotely hosted WebAssembly, `eval`, or `new Function` path. The
dashboard initially showed **Yes** selected on the unsaved draft, so correct this
before saving the Privacy page rather than supplying a remote-code justification.

## Privacy practices

- [x] Conservatively select **all nine** Chrome Web Store user-data categories
      for this capture surface:
  - `Personally identifiable information`
  - `Health information`
  - `Financial and payment information`
  - `Authentication information`
  - `Personal communications`
  - `Location`
  - `Web history`
  - `User activity`
  - `Website content`

  The extension captures arbitrary customer-selected pages under `<all_urls>`,
  including visible screenshots, page/element text, non-password form values,
  selected/copied/pasted text, URLs, titles, referrers, filenames, and interaction
  events. Those payloads can contain any of the nine categories. Password-field
  redaction alone is not a defensible basis for marking another category absent.
- [x] Paste each permission justification without weakening its scope.
- [ ] Certify Limited Use only after product/legal verifies that actual Record
      handling complies with every certification shown in the dashboard.
- [x] Provide the Wexprolabs Record privacy-policy URL describing
      collection, use, transfer to the local native application, configured-model
      and configured-Langfuse processing, retention, deletion, and support access:
      `https://github.com/saminkhan1/record-releases/blob/main/PRIVACY.md`.
      The source policy is retained in
      `release/chrome/WEXPROLABS_RECORD_PRIVACY.md`.
- [x] Ensure the listing and privacy policy accurately disclose collection and
      downstream processing.
- [x] The Desktop start surface and extension popup prominently enumerate the
      captured Chrome and Mac data, the password-value redaction boundary, the
      local-profile destination, and how to end capture.
- [ ] Before submitting, verify the same prominent data disclosure is visible
      in the Store listing before installation and that the dashboard obtains
      affirmative informed consent. Chrome begins enforcing the clarified
      disclosure and necessary-use policy on 2026-08-01.

## Listing assets

- [x] Package icon: `icons/icon128.png`, deterministic 128x128 PNG derived from
      the desktop app icon.
- [x] At least one approved 1280x800 workflow-review screenshot with no customer
      data is uploaded as `cws-assets/workflow-review-screenshot.png`.
- [x] Optional small promotional tile prepared at
      `release/chrome/cws-assets/small-promo-tile.png`. It is a 440x280,
      8-bit-per-channel RGB PNG without alpha. Product approval is still required
      before upload, but the live dashboard does not mark this tile as a
      submission requirement.

Use only the approved no-customer-data workflow-review screenshot and the
verified public privacy URL. The optional promotional tile must not substitute
for the required screenshot.

## Distribution and signed-in acceptance

- [ ] Add the intended tester Google accounts in Publisher settings.
- [ ] Select `Private` and `Only trusted testers from the current publisher`.
- [x] Confirm the name is `Record Chrome Provider` and the description opens
      with `Record Chrome Provider works with the Record Mac application`.
- [ ] Provide reviewer instructions for starting/stopping a local Record
      recording and exercising the extension. Supply test credentials only through
      the dashboard's protected field when they are actually needed.

  Paste-ready instructions (replace the final placeholder with the public,
  signed/notarized download):

  > Requires the signed/notarized Record macOS application. Install and launch
  > Record once, then open Workflows in Record Desktop. Start a recording, visit
  > a public HTTPS page, click or type non-sensitive test text, stop the
  > recording, and verify that a workflow draft appears. The extension popup
  > should show its local native-host connection. No Web Store login is required.
  > Download: `SIGNED_NOTARIZED_DOWNLOAD_URL`

- [ ] Complete Store listing and Privacy practices, then submit for review.
      Private items have the same policy review requirements as public items.
- [ ] After approval, publish to trusted testers from the dashboard.
- [ ] On a clean physical Mac, sign Chrome into an allowlisted tester account,
      launch the signed/notarized Record app once, install/enable the store
      extension, and confirm `chrome://extensions` shows the exact assigned ID in
      `store-identity.json` and version `0.2.2`.
- [ ] If local-file workflows are part of the release, enable **Allow access to
      file URLs** in the extension details and verify one non-sensitive
      `file://` recording. Do not claim local-file capture without that setting.
- [ ] Record a Chrome-to-Mac workflow, stop it, generate a draft, review and
      approve it, then prove one scheduled or triggered execution and its trace.

An anonymous update-service request cannot prove availability of a Private
trusted-tester item. The signed-in clean-Chrome check is the release authority.

Official references:

- [Manifest key and dashboard-assigned identity](https://developer.chrome.com/docs/extensions/reference/manifest/key)
- [Store listing and required assets](https://developer.chrome.com/docs/webstore/cws-dashboard-listing)
- [Privacy fields and permission justifications](https://developer.chrome.com/docs/webstore/cws-dashboard-privacy)
- [Private trusted testers and distribution](https://developer.chrome.com/docs/webstore/cws-dashboard-distribution)
- [Chrome Web Store Limited Use](https://developer.chrome.com/docs/webstore/program-policies/limited-use)
- [Chrome Web Store policy updates effective 2026-08-01](https://developer.chrome.com/blog/cws-policy-updates-2026)
- [Chrome Web Store Program Policies](https://developer.chrome.com/docs/webstore/program-policies/policies)
