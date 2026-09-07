# Create-only notes (`vault_create_note`)

`vault_write` overwrites. That is the right behaviour for an operator editing
their own vault, and the wrong one for an unattended client that files notes on
a schedule: a retry after a dropped response, or two workers picking the same
filename, must never cost an existing note.

`vault_create_note` is the narrow path for that case. It adds a note or it
refuses. It never modifies a file that already exists.

## Guarantees

- **Create-only.** The write goes through `write_bytes_atomic(overwrite=False)`,
  which claims the name with `os.link`. A name that already exists cannot be
  replaced, and the check is the creation itself rather than an `exists()` test
  in front of it, so two concurrent callers cannot both succeed.
- **Verified.** The note is read back and compared byte-for-byte before
  `created: true` is returned. A caller that sees success knows the bytes on
  disk are the bytes it sent.
- **No folder creation.** The configured path pattern says which notes may be
  created, not which folders may appear. A missing parent is
  `parent_folder_missing`, not a new directory.
- **Inert until configured.** With no `VAULT_CREATE_NOTE_PATH_PATTERN` the tool
  returns `create_note_disabled`. An unconfigured policy is not a permissive
  one.

## What it does not give you

The tool constrains *shape*, not *identity*. It runs under the same bearer
token as every other tool, so a client holding that token can still call
`vault_write` and overwrite anything. Treat the policy below as a guard against
a misbehaving client, not as a privilege boundary. Real separation needs
per-client tokens with per-tool scopes, which this server does not have yet.

## Configuration

| Variable | Meaning |
|---|---|
| `VAULT_CREATE_NOTE_PATH_PATTERN` | Regex the vault-relative path must fully match. Empty disables the tool. |
| `VAULT_CREATE_NOTE_REQUIRED_FRONTMATTER` | JSON object `{field: regex}`. Each field must be present and its value must fully match. Map a field to `true` instead of a regex to require it without constraining its value. |
| `VAULT_CREATE_NOTE_ALLOWED_FRONTMATTER` | Comma-separated field allowlist. Empty means any field; non-empty must cover every required field. |
| `VAULT_CREATE_NOTE_ID_FIELD` | Frontmatter field whose value must equal the filename stem. Empty disables the check. |
| `VAULT_CREATE_NOTE_REQUIRE_BODY_SECTION` | Literal string the body must contain, e.g. a heading. |
| `VAULT_CREATE_NOTE_MAX_BYTES` | Per-note ceiling (default `16000`), independent of `VAULT_MAX_CONTENT_SIZE`. |

Values are matched with `fullmatch`, so patterns are anchored without `^`/`$`.

Non-scalar frontmatter values (lists, mappings) have no single string form to
match a pattern against. To require such a field -- `tags`, say -- map it to
`true` rather than to a regex: the field must then be present, and its value is
not inspected.

### Example

An importer that may only file dated task notes into one folder, each carrying
a traceable origin key:

```env
VAULT_CREATE_NOTE_PATH_PATTERN=inbox/tasks/\d{4}-\d{2}-[a-z0-9-]+\.md
VAULT_CREATE_NOTE_REQUIRED_FRONTMATTER={"status":"next","priority":"2|3","source":"importer-doc-[1-9]\\d*-[0-9a-f]{12}","title":true,"tags":true}
VAULT_CREATE_NOTE_ALLOWED_FRONTMATTER=id,title,status,priority,created,updated,tags,source,due
VAULT_CREATE_NOTE_ID_FIELD=id
VAULT_CREATE_NOTE_REQUIRE_BODY_SECTION=## Next Action
VAULT_CREATE_NOTE_MAX_BYTES=16000
```

With that policy, `inbox/tasks/2026-09-check-invoice.md` is accepted only if its
frontmatter carries `id: 2026-09-check-invoice`, a `status` of `next`, a
`priority` of 2 or 3, a `source` matching the origin format, a `title` and
`tags` of any shape, no field outside the allowlist, and a `## Next Action`
section in the body.

## Error codes

| Code | Meaning |
|---|---|
| `create_note_disabled` | No path pattern configured; the tool is inert. |
| `path_not_allowed` | Path does not match the pattern, or violates vault path policy. |
| `note_exists` | A note is already there. Nothing was modified. |
| `parent_folder_missing` | The target folder does not exist. |
| `invalid_frontmatter` | Missing, unterminated, or malformed YAML frontmatter. |
| `frontmatter_missing_field` | A required field is absent. |
| `frontmatter_not_allowed` | A field outside the allowlist was present. |
| `frontmatter_value_rejected` | A required field's value failed its pattern, or is not a scalar. |
| `id_path_mismatch` | The id field disagrees with the filename. |
| `missing_body_section` | The required body section is absent. |
| `content_too_large` | Note exceeds `VAULT_CREATE_NOTE_MAX_BYTES`. |
| `write_verification_failed` | The note did not read back as written. |
| `invalid_policy` | The server's own configuration is malformed (bad regex or JSON). |
| `create_note_failed` | Unexpected failure; details in the server log. |

Rejected values are never echoed back: an error names the field, not the
content, because the content is caller data.

## Client contract

A client that treats any `error` as "outcome unknown" is safe:

1. Create the note.
2. On `created: true`, read it back and proceed.
3. On `note_exists`, the name is taken -- read it and decide whether it is your
   own earlier write or someone else's note. Do not retry with the same name.
4. On any other error, the write did not happen. Retrying the identical call is
   safe, but so is leaving the operation open for a human.

Because a lost response is indistinguishable from a failure at the client, step
3 is what makes a retry harmless: the second attempt either finds its own note
or refuses.

## Enabling and disabling

The tool ships with the server; there is nothing to install. To enable it, set
`VAULT_CREATE_NOTE_PATH_PATTERN` (plus whatever policy you want) in the
server's environment and restart.

To disable it, unset `VAULT_CREATE_NOTE_PATH_PATTERN` and restart. The tool
stays registered and answers `create_note_disabled` for every call, so a client
still configured against it fails loudly instead of silently writing somewhere
unexpected. Notes already created are ordinary vault files and are not touched
by disabling the tool.
