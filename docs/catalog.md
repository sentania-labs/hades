# Catalog

Skills and tools Hades knows. Personas are built from this catalog and scheduled jobs reference tools by name.

## Schema

### Skills

Each skill is a prompt and instructions bundle.

| Field              | Type   | Description                                      |
|--------------------|--------|--------------------------------------------------|
| `name`             | string | Unique identifier for the skill.                 |
| `summary`          | string | Short description of what the skill does.        |
| `owner`            | string | Team or owner responsible for the skill.         |
| `instructions_path`| string | Path to the instructions file, relative to repo root. |

### Tools

Each tool is an MCP server, CLI, or script with its connection details.

| Field            | Type   | Description                                      |
|------------------|--------|--------------------------------------------------|
| `name`           | string | Unique identifier for the tool.                  |
| `kind`           | string | One of: `mcp_server`, `cli`, `script`.           |
| `endpoint`       | string | Required when `kind` is `mcp_server`.            |
| `command`        | string | Required when `kind` is `cli` or `script`.       |
| `credential_ref` | string | Name of the credential (see below).              |
| `allowed_for`    | list   | List of role names. Roles: observer, operator, admin. Absent means all. |

## Credential rule

`credential_ref` is always a plain name string (no spaces, no colons). It references a credential stored in Admin only. The catalog never holds actual secret values.

During load, the loader scans every `credential_ref` against the same secret patterns used by gitleaks (GitHub tokens, JWTs, bearer tokens, AWS keys, etc.). A value that matches any pattern is refused with an error and the catalog fails to load.

This rule exists because a developer who checks in a catalog with a real credential value will cause that value to be scanned and blocked. The credential must be set in Admin (Credentials page) and the catalog only references it by name.

## Roles

The allowed roles for using a skill or tool are listed in `allowed_for`. The three roles are:

- `observer` - read access only
- `operator` - read and execute
- `admin` - full access

When `allowed_for` is absent, all roles may use the entry.

## Endpoints

- `GET /v1/catalog` - returns skills and tools with `used_by` counts (zero when not referenced by any persona)
- `/ui/catalog` - Admin UI page showing both tables, read-only

## used_by

The `used_by` count shows how many personas reference this skill or tool. Currently always zero; it is reserved for the persona builder.
