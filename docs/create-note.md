# Create-only notes (`vault_create_note`)

`vault_write` overwrites. That is the right behaviour for an operator editing
their own vault, and the wrong one for an unattended client that files notes on
a schedule: a retry after a dropped response, or two workers picking the same
filename, must never cost an existing note.

`vault_create_note` is the narrow path for that case, for any client. It adds a
Markdown note or it refuses. It never modifies a file that already exists.

## Guarantees

- **Create-only.** The write goes through `write_bytes_atomic(overwrite=False)`,
  which claims the name with `os.link`. A name that already exists cannot be
  replaced, and the check is the creation itself rather than an `exists()` test
  in front of it, so two concurrent callers cannot both succeed.
- **Verified.** The note is read back and compared byte-for-byte before
  `created: true` is returned. A caller that sees success knows the bytes on
  disk are the bytes it sent.
- **No folder creation.** A missing parent is `parent_folder_missing`, not a new
  directory.
- **Works without configuration.** Unconfigured, any `.md` path the vault's path
  policy allows can be created, with or without frontmatter; a frontmatter block
  that does not parse is refused. That is strictly less than `vault_write` can do
  under the same token, so no policy is needed for safety.

## What it does not give you

The tool constrains *shape*, not *identity*. It runs under the same bearer
token as every other tool, so a client holding that token can still call
`vault_write` and overwrite anything. Treat the optional policy below as a guard
against a misbehaving client, not as a privilege boundary. Real separation needs
per-client tokens with per-tool scopes, which this server does not have yet.

## Optional narrowing

Until v0.15.0 the policy was mandatory and the tool refused everything without a
path pattern. With a policy written for one client, the generic name then invited
every other client into a wall of field-by-field refusals that never said what
would pass. A client with a fixed form is better served by checking its own form
before it calls; the server guarantees the creation. The settings remain for a
vault that wants the server to enforce a shape anyway.

| Variable | Meaning |
|---|---|
| `VAULT_CREATE_NOTE_PATH_PATTERN` | Regex the vault-relative path must fully match. Empty: any `.md` path. |
| `VAULT_CREATE_NOTE_REQUIRED_FRONTMATTER` | JSON object `{field: regex}`. Each field must be present and its value must fully match. Map a field to `true` instead of a regex to require it without constraining its value. |
| `VAULT_CREATE_NOTE_ALLOWED_FRONTMATTER` | Comma-separated field allowlist. Empty means any field; non-empty must cover every required field. |
| `VAULT_CREATE_NOTE_ID_FIELD` | Frontmatter field whose value must equal the filename stem. Empty disables the check. |
| `VAULT_CREATE_NOTE_REQUIRE_BODY_SECTION` | Literal string the body must contain, e.g. a heading. |
| `VAULT_CREATE_NOTE_MAX_BYTES` | Per-note ceiling. `0` (default) means `VAULT_MAX_CONTENT_SIZE`, the limit `vault_write` applies. |

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
| `path_not_allowed` | Not a `.md` path, outside the configured pattern, or against vault path policy. |
| `note_exists` | A note is already there. Nothing was modified. |
| `parent_folder_missing` | The target folder does not exist. |
| `invalid_frontmatter` | Unterminated or malformed YAML frontmatter, or none where a policy requires it. |
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

## Narrowing and widening

The tool ships with the server and works without configuration. To narrow it,
set any of the `VAULT_CREATE_NOTE_*` variables and restart; to widen it again,
unset them. Notes already created are ordinary vault files and are not touched
either way.
