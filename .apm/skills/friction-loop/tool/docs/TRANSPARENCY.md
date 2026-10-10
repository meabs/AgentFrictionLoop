# Transparency and actor pseudonyms

The feedback loop is repository-improvement telemetry, not a people-ranking system.

## Collected

Records contain the schema version, record and commit identifiers, repository/domain, timestamps, changed-path names and aggregate diff statistics, verification status, documentation/guidance metadata and digest, derived observations, optional execution metrics, optional harness telemetry, and an optional pseudonymous `actor` object (`id`, `scheme`, and `kind`).

## Never collected

The system never stores transcripts, prompts, message text, author names, author email addresses, or file contents. Reports and facts contain aggregate actor counts and percentages only; they do not contain actor IDs. There is no per-person ranking, score, leaderboard, or recommendation anywhere in the tool.

## Actor derivation

When a commit author email is available, it is lower-cased and trimmed, then HMAC-SHA256 hashed with the actor salt. The first 12 hexadecimal characters are stored as the actor ID, with scheme `hmac-sha256/v1`; bot-looking addresses are labelled `kind: bot`. Empty or unreadable email produces no actor field.

The salt comes from `FBL_ACTOR_SALT`, when set, otherwise from `config/actor.json` in the insights repository. The store creates that file on first use with a random 32-hex-character salt. A shared salt makes IDs consistent across machines, but this is pseudonymisation, not anonymity: anyone who can test candidate emails against the salt may identify an ID.

## Inspecting your own records

Use:

```sh
python3 review.py --insights /path/to/insights mine --author-email you@example.com
```

The command computes the same HMAC and prints only records attributed to that email, with per-session metrics. It does not reveal other actors' IDs.
