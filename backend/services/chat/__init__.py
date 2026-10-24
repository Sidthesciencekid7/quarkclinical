"""Chat feature (conversational protocol / budget / protocol-edit sessions).

The shared wire contract lives in :mod:`services.chat.contract`. It is the
single source of truth for card, action, spec and SSE shapes; the TypeScript
mirror is ``frontend/src/chat/types.ts``. Nothing is re-exported here on
purpose: importing this package must stay free of side effects so the pure
contract tests (and other agents' pure tests) never pull in the database or
the Anthropic SDK.
"""
