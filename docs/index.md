---
title: "Claude Code forgets a project when you rename its folder"
description: "Claude Code keys auto memory on a project's filesystem path, so moving or renaming the folder silently orphans every note. Here's how to detect it, recover the notes, and stop it recurring."
---

# Claude Code forgets a project when you rename its folder

You reorganise a workspace. Plain `mv` — git untouched, remotes unchanged. The next session inside one of those repos starts from nothing. Not degraded, *blank*: months of accumulated auto memory, and Claude Code behaves like it has never been opened there.

It is not a corrupted install. Auto memory is stored per project under `~/.claude/projects/<sanitized-cwd>/memory/`, and that directory name is derived from the filesystem path of the canonical git root. **The path is the key.** Change the path and a fresh empty store appears. The old notes are still on disk, intact, addressed by a path that no longer exists — and memory files are excluded from the `cleanupPeriodDays` retention sweep, so they are never collected. They just accumulate.

There is no built-in remap. Checked against Claude Code 2.1.233. It has been reported upstream more than once; the most detailed report, [anthropics/claude-code#61349](https://github.com/anthropics/claude-code/issues/61349), was closed unfixed.

Nothing errors when this happens. The model simply no longer knows the project, and you do not know what it forgot.

## Check your own machine

No install. This reads your disk, writes nothing, and takes about a second:

```sh
find ~/.claude/projects -maxdepth 1 -type d | while read -r d; do
  [ -d "$d/memory" ] || continue
  find "$d/memory" -name '*.md' -print -quit 2>/dev/null | grep -q . || continue
  t=$(find "$d" -maxdepth 1 -name '*.jsonl' -print -quit 2>/dev/null)
  [ -n "$t" ] || continue
  cwd=$(head -1 "$t" | sed -n 's/.*"cwd":"\([^"]*\)".*/\1/p')
  [ -n "$cwd" ] && [ ! -d "$cwd" ] && echo "orphaned: $cwd"
done
```

Every line it prints is a project whose notes exist but can no longer be reached. If it prints nothing, no repo has moved since you started using Claude Code, and none of this affects you yet.

### Why it reads the transcript instead of decoding the directory name

The obvious approach — decode `<sanitized-cwd>` back into a path and test whether it exists — does not work, and fails in the direction that matters. **The encoding is lossy:** `-` maps back to both `/` and a literal hyphen, so a repo named `my-project` and a directory `my/project` encode identically. A decoder built that way reported 289 of 303 directories as orphaned on the machine this was written on. Nearly all of them were false.

The authoritative source is the `cwd` field on the first line of any transcript `.jsonl` in the same directory. It records the real path, with no decoding involved. A detection pass that guesses is worse than none, because it teaches you to ignore it.

## The fix was already in the product

`autoMemoryDirectory`, set per project:

```jsonc
// <project>/.claude/settings.local.json
{ "autoMemoryDirectory": "/absolute/path/to/notes/for-this-project" }
```

The store location stops being *derived* from the working directory and becomes *declared* by a file that lives inside the project. Rename the directory, move it, clone it again — the settings file travels with it and keeps pointing at the same notes. Path-derived keying breaks precisely because the key is computed from something you are free to change.

Two caveats before adopting it:

- Point several checkouts of one repo at a single directory deliberately. Keying on the git **remote** means every worktree and second clone shares one store instead of accumulating a fresh one each.
- If a repo tracks `.claude/settings.local.json` upstream, writing an absolute path into it commits your machine's filesystem layout to a shared repo. `git update-index --skip-worktree` avoids that — and verify the bit actually took, since `update-index` on a path missing from the index exits 0 without setting anything.

## Commonbook

[Commonbook](https://github.com/erfanhabibipanah/commonbook) automates the above: it finds orphaned stores, binds every repo to a directory keyed on its git remote, and reports what is bound, what is stale, and what was lost.

```sh
curl -fsSL https://github.com/erfanhabibipanah/commonbook/releases/latest/download/commonbook.pyz -o commonbook.pyz
python3 commonbook.pyz doctor
```

Python 3 standard library only. No dependencies, no install step, no network access at runtime. MIT licensed.

`doctor` reports; nothing writes to your notes unless you ask it to. `prune` refuses to delete the only copy of a store rather than guessing.

- **[Source and full README](https://github.com/erfanhabibipanah/commonbook)**
- **[The long version of this writeup](the-bug.html)**
