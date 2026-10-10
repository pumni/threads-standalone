# SPLIT-04 journaled mutation parity

Frozen behavior source: `pumni/thread@0dfbd36ff3756f8a68edcd6cc0bd083f6f3570a1`; destination base: `pumni/threads-standalone@fbd7c8591460e477cf15ac2b86d864c3c5189751`. The package keeps the `threads_platform` namespace and account v1 schema. All automated checks use synthetic identifiers, temporary files, and fake HTTP responses. This document does not claim live verification.

## Supported commands and runtime surface

| Command or method | Runtime/API | Synthetic coverage |
| --- | --- | --- |
| `threads-local post ALIAS TEXT` | `publish_text` → `create_container` → one `publish_container` | `tests/test_mutation_cli.py`, `tests/test_mutation_runtime.py`, `tests/test_mutation_api.py` |
| `threads-local reply ALIAS THREAD_ID TEXT [--parent-reply-id ID]` | `create_reply`, optional parent ID and reply quota | Same files; reply quota and failure-boundary cases in runtime tests |
| `threads-local post-image ALIAS IMAGE_URL [TEXT] [--alt-text TEXT]` | Image container, bounded processing poll, publish | Runtime and fake HTTP tests |
| `threads-local post-video ALIAS VIDEO_URL [TEXT] [--alt-text TEXT]` | Video container, bounded processing poll, publish | Runtime and fake HTTP tests |
| `threads-local post-carousel ALIAS MANIFEST` | Ordered 2–20 child containers, parent, one publish | Manifest, order, bounds, child and parent failure cases in runtime tests |
| `threads-local moderate-reply ALIAS REPLY_ID {hide,unhide,approve,ignore}` | `manage_reply` or `manage_pending_reply` | Runtime and fake HTTP action mapping tests |
| `threads-local operation show OPERATION_UUID` | Local journal read only | CLI test verifies no API runtime is constructed |
| Runtime `publish_quote` | Quote container with `quote_post_id`; journal kind `POST_QUOTE` | Runtime tests; intentionally no direct CLI quote command |

The already accepted account, API READ, and browser commands remain in place. API READ commands do not import or launch the browser. No Controller, Worker, PostgreSQL, or old-repository runtime dependency is introduced.

## Journal v1 and recovery boundary

The local journal remains `<data-root>/operations/<operation_uuid>.json`; process locks remain `<data-root>/locks/<account_uuid>.lock`. The exact v1 required fields are `version`, `id`, `account_id`, `kind`, and `phase`. Its only optional fields are `container_id`, `media_id`, `outcome_code`, `child_container_ids`, and `action`. Records are strict-key, bounded to 8192 bytes, validated before use, and atomically created or replaced. Carousel child IDs are append-only, ordered, unique, and limited to 20. Account IDs, operation IDs, phases, and provider identifiers are immutable where the source contract requires them.

The seven supported journal kinds are `POST_TEXT`, `POST_QUOTE`, `CREATE_REPLY`, `POST_IMAGE`, `POST_VIDEO`, `POST_CAROUSEL`, and `MODERATE_REPLY`. Publish phases preserve `RECEIVED`, carousel `CHILDREN_CREATING`, `CONTAINER_CREATED`, `PUBLISH_REQUESTED`, `PUBLISHED`, `CONFIRMED`, `AMBIGUOUS`, and `FAILED_FINAL`. Moderation records progress through `RECEIVED`, `MUTATION_REQUESTED`, and `CONFIRMED`, or terminate as `AMBIGUOUS` when the result cannot be established.

Publishing quota is read once before journal creation or container creation; reply quota is checked for replies. Media processing is bounded to 10 polls and 30 seconds. The journal is durably advanced to `PUBLISH_REQUESTED` before the sole publish request, and to `MUTATION_REQUESTED` before moderation. A timeout, cancellation, transport uncertainty, invalid response after that boundary, or persistence failure after a remote effect is treated as ambiguous. It is recorded as such when possible, and no publish or moderation request is retried. A deterministic publish rejection may trigger at most one read-only container status reconciliation under the source classifier. The operation journal records progress for operator review; it does not provide an automatic restart/replay mechanism.

`operation show` reads the local journal only and prints bounded operation metadata. It never resolves credentials or calls the API. The full implementation and failure matrix are in `src/threads_platform/standalone/mutations.py`; HTTP mutation methods are isolated in `src/threads_platform/api/client.py` and their data/protocol contracts in `src/threads_platform/api/contracts.py`.

## Evidence labels

- **IMPLEMENTED**: the CLI routes the listed command to the local mutation runtime, which enforces journal v1, account process locking, source validation, quota ordering, one-request publish/moderation boundaries, and source error classification.
- **SYNTHETICALLY TESTED**: runtime failure/recovery coverage is in `tests/test_mutation_runtime.py`; fake HTTP route/parameter/error coverage is in `tests/test_mutation_api.py`; CLI dispatch and local `operation show` coverage is in `tests/test_mutation_cli.py`. The earlier 134 destination synthetic tests remain present.
- **NOT LIVE-VERIFIED**: no Threads publish, reply, moderation, or other remote mutation was performed. No token, live account data, browser profile, local journal, or private evidence was read or copied.
- **NOT INCLUDED**: Nurture and Workflow composition remain for a separately authorized checkpoint; Controller, Worker, PostgreSQL, and migrations are not part of this port.
