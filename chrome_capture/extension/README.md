# Record Chrome Provider

> Manifest V3 Chrome extension for demonstrated browser workflow capture in [Record](https://github.com/saminkhan1/record-plugin).

The **Record Chrome Provider** records user browser interactions (clicks, form inputs, navigations, downloads) during an active Record recording session on macOS and streams structured interaction evidence to the local Record native messaging host.

---

## Overview

- **Extension Name**: Record Chrome Provider
- **Manifest Version**: 3
- **Primary Purpose**: Capture demonstrated user actions and DOM evidence in Google Chrome exclusively while a local macOS Record session is active.
- **Integration**: Communicates with the local Hermes Record runtime via Chrome Native Messaging (`~/.hermes/record/chrome-provider/`).

---

## Security & Privacy Controls

1. **Active-Only Capture**: Inactive content scripts do not read, buffer, or emit page data. Captures are strictly bound to an active, sealed recording session.
2. **Password & Sensitive Field Redaction**: Input content from password fields, hidden tokens, and sensitive credential elements is automatically redacted before transmission.
3. **No Dynamic Execution**: Manifest V3 compliant. Absolutely no remote scripts, `eval()`, or dynamic script injections.
4. **Isolated Native Host**: The native messaging host accepts connections only from the authorized Chrome Web Store extension origin and validates active session tokens.

---

## Architecture

```text
Google Chrome (MV3)                             macOS Host
┌─────────────────────────────────┐           ┌─────────────────────────────────┐
│  Content Scripts                │           │  Native Messaging Host          │
│  - causality.js                 │  port     │  - native_host_launcher.py      │
│  - user-events.js               │──────────>│  - native_host.py               │
│                                 │           └────────────────┬────────────────┘
│  Service Worker                 │                            │
│  - background.js                │                            ▼
│                                 │           ┌─────────────────────────────────┐
│  Popup Surface                  │           │  Record Engine                  │
│  - popup.html / popup.js        │           │  (~/.hermes/record/recordings/) │
└─────────────────────────────────┘           └─────────────────────────────────┘
```

- `src/user-events.js`: Captures user interactions (clicks, inputs, submissions, change events) and DOM context with privacy redacting.
- `src/causality.js`: Correlates user actions with subsequent navigations and downloads.
- `src/background.js`: Manages the lifecycle, listens for extension state changes, and establishes the native-messaging connection.
- `src/popup.html` & `src/popup.js`: Displays real-time recording status and connection readiness to the user.

---

## Development & Testing

### Prerequisites
- Node.js >= 20

### Syntax Check
Verify that all source scripts parse cleanly:
```bash
npm run check
```

### Run Tests
Execute the unit test suite (DOM event listeners, privacy redactions, native message queueing):
```bash
npm test
```

### Load Unpacked into Chrome
1. Open Google Chrome and navigate to `chrome://extensions/`.
2. Enable **Developer mode** (toggle in the top-right corner).
3. Click **Load unpacked** and select this directory (`chrome_capture/extension`).
4. Ensure the extension ID matches the key defined in `manifest.json`.

---

## Packaging

Packaging is automated from the repository root:
```bash
# From repository root:
npm run package:chrome
```

This validates `store-identity.json`, verifies the SHA-256 hashes of all assets, and creates a deterministic release ZIP in `dist/chrome/` ready for Chrome Web Store submission.
