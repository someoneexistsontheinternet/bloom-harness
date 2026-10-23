# First-draft limitations

- The supported local environment is Apple Silicon macOS/Python 3.12. Native
  permissions, Safari, launchd, sandbox-exec and the pinned command tool are
  platform-dependent. Other environments are not certified.
- Astra and Sol default to full developer access. This is a trusted personal
  runtime, not an adversarial multi-user sandbox or hosted bot platform.
- Model IDs, context capacity, reasoning effort and provider capabilities must
  be configured and verified. A local 1M setting does not grant model access.
- The Codex-auth adapter and browser-inference route depend on external tools/UI
  and are not portable public-API guarantees. Browser inference has no default
  model/account selection. MCP client attachment is explicit.
- Sol task/session records persist, but reusable independent Sol personalities,
  private long-term memories and per-server Discord identities are future work.
- Memory quality remains a research/iteration area. Source validation and exact
  Astra approvals prevent some failures; they do not guarantee perfect retrieval
  or useful consolidation. Retrieval is lexical/hierarchical, not embeddings.
- Search visits candidates sequentially. Full discussions can be legitimately
  large; duplicate DOM inventories are filtered, but tokens and latency are
  still workload-dependent. PDF extraction is unsupported.
- macOS permission switches alone do not prove GUI-service access. Ad-hoc helper
  signing changes may require new grants. Native screenshot/desktop behavior
  across every display/profile is not verified.
- GUI services do not inherit terminal credentials automatically. There is no
  interactive provider-key installation wizard or multi-installation service
  namespace manager in this draft.
- Cold-source bytes require the mounted archive unless cached. Local search
  remains available, but its index/catalog must be backed up and preserved.
- Tests and local checks are evidence for specific mechanisms. They do not
  certify every provider, private website, SSH target or Discord deployment.

Issue reports should include a minimal, redacted reproduction and the exact
revision/environment, rather than a private workspace or conversation dump.
