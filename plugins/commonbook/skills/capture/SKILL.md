---
name: capture
description: Write a durable note into this project's book — a decision with the alternative that lost, a gotcha with the error text that identifies it, or a plain fact worth keeping. Use the moment something is learned that a future session would otherwise rediscover.
license: MIT
---

# Capture

Write one note into the project's book. `commonbook caps` gives you the
directory; if the repo is unbound, run `commonbook bind` first and say so.

Pick the type honestly. The type is not decoration — each one has a requirement,
and a note that cannot meet it is a different type.

## decision

A choice between real options.

**Required: the alternative that lost, and why.** This is the entire reason the
note exists. In six months the chosen path is visible in the code; what is
invisible is that you already considered and ruled out the other one, which is
what stops it being relitigated.

If nothing was rejected, this is not a decision. It is a preference or a
default — capture it as a note instead.

Also record what would make this wrong: the scale, version or price change that
should trigger a revisit.

## gotcha

Something that cost real time.

**Required: the error text, verbatim, in a fenced block.** Retrieval is a literal
string search, so a paraphrased or tidied error is an unfindable one. Copy it
exactly, including the parts that look like noise.

Then: the actual cause — not the first theory you abandoned — and the fix. Add
what you tried that did *not* work; that section is usually worth more than the
fix, because it is what stops the next person walking the same path.

## note

Anything else worth keeping: a constraint, a fact about a service, where things
stand.

## Rules for all three

- Write nothing a reader could get by opening the code. No summaries of what
  functions do.
- Never write credentials, tokens or keys into a note.
- Prefer editing an existing note over adding a near-duplicate.
- Keep `MEMORY.md` an index — one line per entry, detail in topic files. It is
  loaded into every session and is capped; long entries push later ones out.

Show the finished note and where it was written.
