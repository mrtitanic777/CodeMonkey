---
name: api-design
description: Use when you're shaping a new HTTP endpoint, library function, or data format and want the interface to be consistent, versionable, and hard to misuse.
keywords: [api, endpoint, interface, rest, versioning]
---

# API Design

You design interfaces for their readers and their future, not for the
implementation's convenience. A good API is obvious at the call site,
consistent everywhere, and hard to use wrong. Sketch the interface
first — names, shapes, errors — before writing any implementation.

## Naming and structure

- **Resources are nouns, actions are rare verbs.** `POST /orders`,
  not `POST /createOrder`. Reserve verbs for things that aren't
  resource operations (`POST /orders/123/cancel`).
- **One convention everywhere.** If it's `user_id` in one endpoint
  it's `user_id` in all of them — never `userId` next door. Pick
  casing, pluralization, and date formats once and enforce them.
- **Nest shallowly.** `/orders/123/items` is fine;
  `/users/4/orders/123/items/9` means the modeling is wrong.

## Versioning and compatibility

- **Version from day one.** A `/v1` prefix or versioned media type
  costs nothing now and saves a migration later.
- **Additive changes only.** New fields and new endpoints are safe;
  renaming, removing, or retyping a field breaks clients. Deprecate
  with a sunset date instead of removing.
- **Defaults are a contract.** Changing a default is a breaking
  change wearing a disguise.

## Requests and responses

- **Validate fast, fail clearly.** Reject bad input at the boundary
  with a message that says what's wrong and what's expected. Never
  trust the client to send what you hope for.
- **One error shape.** Same envelope everywhere: a stable machine
  code, a human message, and details. Use real status codes —
  `400` for bad input, `404` for missing, `409` for conflicts,
  `422` when it's valid JSON but semantically wrong.
- **Paginate everything unbounded.** Cursor-based for large or
  growing sets; always cap page size server-side.
- **Make retries safe.** Mutations that clients may retry get
  idempotency keys; `POST` that creates twice on retry is a bug in
  your design, not the client's.

## Hard to misuse

- Required parameters are obviously required; dangerous operations
  are explicit (no `?really=true` hidden flags — name the thing
  what it does).
- Safe defaults: the default path should be the safe path.
- Document with examples, not just schemas. One realistic
  request/response pair teaches more than a field table.

## Review checklist

Before finalizing: names consistent with the existing API? Versioned?
Errors uniform? Pagination on lists? Retry-safe mutations? Additive
only? If any answer is no, fix the design — implementation hasn't
started yet, so changes are still free.
