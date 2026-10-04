# Security and private data

EvoG is a local command-line framework, not an authentication service. The deployment must authorize
the groups supplied to a query and isolate workspaces between tenants. Navigation notes are
scoped by the exact authorized group set. Source context is read-only through agent tools.
Path traversal and resolved symlink escapes are rejected by the logical file boundaries.

Conversation text is untrusted. Prompts tell the model to treat embedded instructions as data;
the fixed runtime separately limits source scope, writable notes and supported citations.
These checks do not guarantee semantic correctness or eliminate every prompt-injection effect.

Live model requests transmit authorized conversation evidence to the configured provider.
Assess its data policy before use. Do not point a credential-bearing API configuration at an
untrusted endpoint. HTTP is supported for local services; use HTTPS for remote deployments.

The local database and trace exports contain message content. The database is owner-only on
POSIX and private workspaces are ignored by Git. Manage backups, retention and trace-export
permissions at deployment level. Logs omit provider headers and error bodies; source messages
are not automatically scrubbed for secrets.

Automatic evolution can change only bounded harness files, never generated Python or deployment
settings. Identifier/verbatim-copy screening is a guard, not a proof of task generality. Use a
business validator and inspect proposed content before relying on it in an application.

For a vulnerability, contact the repository owner privately using their GitHub profile instead
of posting private conversations or credentials in a public issue. Include a synthetic reproducer
and affected version. This repository does not currently advertise a dedicated security inbox.
