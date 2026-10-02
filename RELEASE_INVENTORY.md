# Release inventory and provenance

This release exports the Perpetua CLI and supervisor, Python libraries, starter templates, fictional disabled backend example, model-free lifecycle tests, crash-recovery demonstration, CI workflow, and MIT license. The source was adapted from an existing private development checkout into a fresh history. Runtime paths and optional personal integrations were made configurable for the standalone release.

Original goals, backend configuration, session transcripts, logs, deployment units, notification credentials, databases, private Git history, and local modifications outside the explicit source export are excluded. Source and runtime roots are separate; test and demo state lives in temporary directories. The recovery demo runs the real supervisor with fake external commands and verifies interrupted-process reconciliation; it does not establish behavior against every real agent or service implementation.

The portfolio map provides public project context. No model assets or third-party implementation code are bundled. Generated runtime state and Python build/cache artifacts are excluded by `.gitignore`.
