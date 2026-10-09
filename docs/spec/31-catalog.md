# 31. Catalog of skills and tools

## Overview

The catalog lives at `config/catalog.yaml`. It declares every skill and tool
Hades knows. The persona builder reads it to compose agent behaviour;
scheduled jobs reference tools by name.

## Schema

### skills

Each skill is a prompt and instruction bundle.

| Field              | Type   | Required |
|--------------------|--------|----------|
| name               | string | yes      |
| summary            | string | yes      |
| owner              | string | yes      |
| instructions_path  | string | yes      |

### tools

Each tool is an MCP server or a CLI/script with its credential reference.

| Field           | Type   | Required | Notes                           |
|-----------------|--------|----------|---------------------------------|
| name            | string | yes      | Unique within the catalog.      |
| kind            | string | yes      | One of `mcp_server`, `cli`, `script`. |
| endpoint        | string | conditional | Set when kind is `mcp_server`.     |
| command         | string | conditional | Set when kind is `cli` or `script`. |
| credential_ref  | string | yes      | A name only, never a value. See rules below. |
| allowed_for     | array  | no       | List of role names. Absent means all roles. |

## Credential rules

- `credential_ref` holds a **name only**. It references a credential stored
  in Admin only (the credential store). Actual values are never placed in
  the catalog, in the database, in API payloads, in images, in git, or in
  logs.
- The loader rejects any `credential_ref` that matches a secret pattern
  from the gitleaks ruleset: base64-encoded tokens, `sk-`, `ghp_`,
  `ghs_`, `eyJ`, `AKIA`, `sk-ant-`, `xoxb-`, `ya29.`, `1//`,
  `Bearer` headers, PEM private-key headers, and Crucible tokens
  (`cru_`).
- A `credential_ref` that contains whitespace or colons is also refused;
  it must be a plain name like `claude_code_credential`.

When a persona references a tool, it uses the tool's `name`. The runtime
resolves that name to the credential reference and mounts the credential
value from Admin only at launch time.

## used_by

The `used_by` field on every entry counts how many personas currently
reference it. It is zero at initial load because no persona builder exists
yet. The field is included in the API and the admin UI so it can be wired
up later without a schema change.

## API endpoint

`GET /v1/catalog` returns a JSON object with `skills`, `tools`, and
`errors` (a list, empty on a successful load). Observers may read it.

## Admin UI page

`/ui/catalog` shows two read-only tables. No forms, no mutations.
Registered without a navigation link (the page is accessible directly).
