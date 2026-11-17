# Security policy

## Supported version

Security fixes target the latest `1.2.x` IINA integration on the default branch.
The upstream system-audio overlay is retained; changes to it are reviewed as part
of this repository, but this project cannot provide fixes for upstream dependencies.

## Reporting a vulnerability

Use **Security → Report a vulnerability** on GitHub when private reporting is
available. Otherwise, contact the repository owner through the contact information
on their GitHub profile. A public issue may request a private reporting channel,
but should not contain exploit details, recordings, transcripts, tokens or private
file paths.

Include the affected version, relevant configuration, reproduction steps, impact,
and a minimal example. Please allow time for a fix before public disclosure.

## Local trust boundary

The IINA worker accepts authenticated requests on loopback, reads a selected local
video and writes subtitle caches in the user's Application Support directory.
Its token is generated during installation and is not part of the repository.
Ollama is a separate local service. Models must be downloaded before offline use.

The installed plugin uses IINA's loopback network and OSD permissions. On IINA
1.5+, native subtitle-file commands also require the filesystem permission. The
installer retains that permission for those versions while removing direct
JavaScript filesystem calls from the installed bridge.

Reports involving unauthorized file access, token disclosure, remote exposure, or
unsafe installer behavior are in scope. Translation inaccuracies and transcription
hallucinations normally belong in a regular bug report.
