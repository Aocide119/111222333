# Group memory

Original conversation records remain the primary evidence. A stored note is a navigation aid and must carry a source reference and an extract that can be checked against the original record.

Use group_profile for group scope and shared roles, people for stable individual roles or preferences, and events for dated decisions and changes. Temporary searches and unresolved questions belong in working_memory and remain isolated to their question.

Each round reads a frozen group memory snapshot. Writes are staged per question and merged deterministically at the round boundary after provenance validation. Do not store reference answers, evaluation scores, API credentials, or facts inferred without source evidence.
