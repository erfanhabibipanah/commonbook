---
name: recall
description: Search this project's book of durable notes before solving something. Use when the user asks whether a problem has come up before, why something is built the way it is, or when an error looks familiar — and before proposing an approach on a project that has history.
license: MIT
---

# Recall

Search the project's book before re-deriving something.

Run `commonbook caps` to find the book. It prints JSON: `book` is the directory,
or `null` when this repo is unbound.

**If `book` is null**, say so plainly, search the repo with grep instead, and
mention that `commonbook bind` would give this project a durable book. Do not
invent a location.

**If `book` is set**, search it in this order and stop as soon as you have enough:

1. **The literal error.** If the user pasted an error, grep the exact string.
   Notes are written to be found this way, so a verbatim match is the highest
   signal available.
2. **Decisions.** Look for notes recording a choice. Check whether one has been
   superseded — a stale decision presented as current is worse than no answer.
3. **The index.** `MEMORY.md` lists the topic files. Read only the ones that
   match; topic files are not loaded automatically and are often long.
4. **Everything else**, only if the first three found nothing.

Then report what you found, quoting the relevant lines with the file each came
from. Say which notes look stale.

**If nothing matches, say that.** "No prior note on this" is a useful answer and
a fabricated recollection is not. Do not pad the response with plausible-sounding
history that is not in the book.
