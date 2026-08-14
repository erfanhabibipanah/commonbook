---
name: bind
description: Set up or repair this project's memory binding — bind an unbound repo, find memory orphaned by a move or rename, and report what is at risk. Use when memory seems to have been forgotten, after moving a project, or when setting up a new machine.
license: MIT
---

# Bind

Claude Code stores a project's auto memory in a directory named after the
project's **filesystem path**. Move or rename the project and a fresh, empty
store appears; the old notes stay on disk but nothing can reach them again.

Commonbook points that storage at a directory keyed on the repo's git remote
instead, so it survives moves, renames and fresh clones, and every worktree of
the repo shares one book.

## Start here

```
commonbook doctor
```

It reports whether this repo is bound, whether any memory on this machine is
orphaned, and anything that puts a book at risk. Read its output before acting.

## The three fixes

**Unbound repo** — memory works but is keyed on the path, so it will be lost on
the next move:

```
commonbook bind
```

**Orphaned memory** — notes exist on disk that no session can reach:

```
commonbook adopt --dry-run     # see what would be recovered
commonbook adopt               # copy it into each project's book, then bind
```

Adopted notes land *beside* the existing ones, never merged into them. Merging is
the one operation that loses information without saying so.

**Already adopted** — the originals are still on disk:

```
commonbook prune --dry-run
```

`prune` deletes an original only after confirming every note it holds is present
in the book. It refuses otherwise, because those notes exist nowhere else.

## Reading the output

- `identity: remote` — good. The book follows the repo.
- `identity: root-commit` — no remote yet; stable, but add one.
- `identity: path` — **not stable.** This book will orphan again on the next
  move. Adding a git remote fixes it.

To keep books in a synced or version-controlled directory, pass
`--vault <dir>` to any command. That makes the notes portable across machines;
without it they are local to this one.
