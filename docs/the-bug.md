# Claude Code forgets a project when you rename its folder

I regrouped a workspace recently. Twenty-odd repositories that had been sitting flat at the top went into subdirectories. Plain `mv`. Git untouched, remotes unchanged.

The next session inside one of those repos started from nothing. Not degraded — blank. Claude Code had been accumulating auto memory in that project for months and behaved like it had never been opened there. My first guess was a corrupted install.

It isn't. Auto memory is stored per project under `~/.claude/projects/<sanitized-cwd>/memory/`, and that directory name is derived from the filesystem path of the canonical git root. **The path is the key.** Move the directory, the sanitized name changes, and a fresh empty store appears. The old one is still on disk with every note intact, addressed by a path that no longer exists.

I went looking for a remap — a rename hook, a migrate command, a fallback that matches on the remote. There is none. Memory files are also excluded from the `cleanupPeriodDays` retention sweep, so orphans are never collected. They just accumulate.

## Check your own machine

This reads your disk, writes nothing, and takes about a second:

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

Every line it prints is a project whose notes exist but can no longer be reached. If it prints nothing, you have never moved a repo since you started using Claude Code, and none of this affects you yet.

On my laptop, one afternoon of tidying produced 9 orphaned stores — 33,979 bytes across 5 projects.

## The fix was already in the product

`autoMemoryDirectory` is a supported setting, resolved from user, project, local and policy scope. Written into a repo's `.claude/settings.local.json` it travels with the directory, because it *is* in the directory. The store stops being derived from a path that changes and becomes configuration that moves with the code.

You can write that line by hand today, and if you only have one repo, you probably should.

[Commonbook](https://github.com/erfanhabibipanah/commonbook) is what I wrote for the case where you have thirty. It keys the directory on `sha256` of the repo's normalised git remote, which gives three properties the default lacks: it survives `mv` and rename; it is re-derivable after a fresh clone, since the same remote yields the same id; and every worktree and second checkout share one book, because the ssh, https and scp-style forms of a remote all normalise to one identity. Today a second clone gets a second brain.

Five commands. `doctor` reports what is bound, unbound, orphaned or at risk, and exits non-zero. `bind` points a repo at its book. `adopt` finds orphaned memory, copies it into the right book, and binds the repo so it cannot recur. `prune` deletes originals only after verifying each note is present in the book. `lint` enforces the typed-note contracts — it found 11 violations in a 102-note directory of mine.

## The part that was actually hard

Not the binding. Finding orphans without lying about them.

The obvious approach is to decode the directory name back into a path. It does not work: the encoder maps a hyphen to both a path separator and a literal hyphen, so `client-api` and `client/api` encode identically. Naive decoding reported about 95% of my live projects as orphans. The authoritative source is the `cwd` field on the first line of each transcript `.jsonl`. Read that instead and the false positives disappear.

Two behaviours are deliberate. Adopted notes land *beside* existing ones and are never merged, because merging is the one operation that loses information without saying so. And `prune` refuses to delete any store whose notes it cannot verify are in the book — those notes exist nowhere else, so the destructive path fails closed.

## What it doesn't do

It reduces loss, not consumption.

Building an aggregate index across books found 366 notes, indexed 151, and dropped 215 at the 200-line / 25,000-byte session-load cap. Concatenation was never possible at that scale, and the drop count is printed rather than silently truncated — an index that omits entries while looking complete is worse than one that admits it is partial.

Anthropic could also ship a first-party remap tomorrow and make `bind` unnecessary. The open upstream request for cross-project memory suggests the gap is noticed rather than unnoticed.

And Commonbook is not in the read path. Notes stay plain markdown in a plain directory; delete the tool and everything is still readable by anything. That is the design, not a limitation — a memory tool you cannot leave is a worse problem than the one it solves.

---

777 lines, one file, standard library only. No dependencies, no network calls. 39 tests run against a temporary `HOME` so a bug cannot reach a real memory store, and CI covers Ubuntu, macOS and Windows against Python 3.9 and 3.13, plus a check that fails on any non-stdlib import — because the pitch is that you curl one file and run it.

If the snippet above printed anything, `commonbook adopt --dry-run` will tell you what it can recover before it touches a thing.
