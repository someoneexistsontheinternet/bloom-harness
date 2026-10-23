# Security and privacy

Bloom is a personal runtime for a trusted owner. Astra and Sol can have full
developer access under the current account. Workspace/read modes constrain
supervised tools but are not a guarantee against malicious instructions or
every effect of already-running GUI applications.

Credentials belong in the configured environment or credential store. Local
configuration stores names/paths, not tokens. Never share credential files,
complete environment dumps, private Keychain output or a populated workspace.
Known-value redaction reduces accidental disclosure; it is not proof that every
possible secret has been detected.

Web pages, files and worker reports are untrusted evidence. Signed-in Safari
captures can contain private content, which is sent to the selected model
provider for review. The optional Discord activity feed can publish detailed
tool arguments/results; use a channel whose access matches that content.

Conversation databases, originals, backups, screenshots and archive indexes
contain private data. They are deliberately excluded from source control.
Preserve uncertain effect receipts rather than blindly replaying operations.

## Reporting

Use GitHub's private vulnerability reporting feature if it is enabled on the
repository. Otherwise ask the maintainer for a private reporting channel without
including exploit details or secrets in a public issue. Do not attach a live
workspace, authentication material or unredacted model/tool transcripts.

No supported-version response SLA or external security audit is claimed for
this first draft.
