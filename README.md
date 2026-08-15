# Commonbook

[![CI](https://github.com/erfanhabibipanah/commonbook/actions/workflows/ci.yml/badge.svg)](https://github.com/erfanhabibipanah/commonbook/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/erfanhabibipanah/commonbook)](https://github.com/erfanhabibipanah/commonbook/releases/latest)
[![License](https://img.shields.io/github/license/erfanhabibipanah/commonbook)](LICENSE)

Commonbook rekeys Claude Code's auto memory from the filesystem path of a project to its git remote, so the memory survives `mv`, rename and re-clone — and it recovers the notes that path-keying already lost.

Claude Code stores each project's auto memory under `~/.claude/projects/<sanitized-cwd>/memory/`, in a directory named after the project's **path**. Move or rename the folder and the next session starts from an empty store. The old notes stay on disk, addressed by a path that no longer exists, and the retention sweep never collects them. There is no built-in remap (checked against Claude Code 2.1.233). It has been reported upstream more than once; the most detailed report, [anthropics/claude-code#61349](https://github.com/anthropics/claude-code/issues/61349), was closed unfixed. Nothing errors when it happens — the model simply no longer knows the project, and you do not know what it forgot.

The full story of the bug, and how the orphan detection avoids lying about it, is in **[docs/the-bug.md](docs/the-bug.md)**.

---

## Check your machine first

No install required. This reads your disk, writes nothing, and takes about a second:

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

Every line it prints is a project whose notes exist but can no longer be reached. If it prints nothing, no repo you use has moved since you started using Claude Code, and none of this affects you yet.

A single line is fixable by hand: copy `~/.claude/projects/<old-encoded-path>/memory/` into the store for the new path, and you are done — no tool needed. Commonbook is for the other outcome. On the machine it was written on, one afternoon of workspace tidying printed nine — and after that afternoon, for never needing to run this check again.

---

## Install

**As a Claude Code plugin**

```
/plugin marketplace add erfanhabibipanah/commonbook
/plugin install commonbook@commonbook
```

Then, in any repo: `/commonbook:bind`

**As a standalone command** — one file, standard library only, no dependencies:

```sh
curl -fsSLO https://github.com/erfanhabibipanah/commonbook/releases/latest/download/commonbook.pyz
curl -fsSL https://github.com/erfanhabibipanah/commonbook/releases/latest/download/commonbook.pyz.sha256 | shasum -a 256 -c -
install -m 755 commonbook.pyz ~/.local/bin/commonbook
```

(`sha256sum -c -` is the Linux spelling of the second line.) Requires Python 3.9+ and git. Works on macOS, Linux, WSL and Windows — the single file runs anywhere Python does. The plugin's `commonbook` shim is a POSIX shell script, so on native Windows use the downloaded file directly.

You are being asked to point a downloaded file at the directory holding months of notes, so the trust claims here are ones you can check rather than take. The file is built from this repository by `python3 build.py`; CI builds it the same way and runs every verb this README names against the artifact, so the documented install cannot quietly lose a command. CI separately fails the build on any import outside the standard library, and a grep of the source for `urllib`, `socket` or `http` comes back empty — there are no network calls at runtime. The test suite runs against a temporary `HOME` on Ubuntu, macOS and Windows, Python 3.9 and 3.13, so a bug in a test can never reach a real memory store.

---

## First run

```console
$ commonbook doctor

  repo      ~/dev/api-gateway
  identity  remote  github.com/you/api-gateway
  binding   unbound — memory is keyed on this path and will
            orphan if the directory moves. Fix: commonbook bind
  …
```

`commonbook doctor` reports this repo's binding, then continues machine-wide: every store that holds notes, what is orphaned and recoverable, what is already adopted and safe to prune. It exits non-zero when something needs fixing, so it can gate a script. `commonbook bind` writes the binding — a single supported setting, described under How it works — and running it in an already-bound repo says so and changes nothing.

---

## Recover what was already lost

```console
$ commonbook adopt --dry-run

found 9 orphaned memory stores
  …

dry run — would adopt 9 stores, 33,979 bytes, 5 projects
```

Those are the real numbers from the afternoon that prompted the tool; all nine stores came back. The bytes are small because notes are dense. What they held was months of corrections — conventions, dead ends, verbatim error strings — that would otherwise be re-taught one mistake at a time, with no error ever shown.

`commonbook adopt` matches each orphan to a live repo, copies its notes into that repo's book, and binds the repo so this cannot recur. The matching rule is worth stating so you do not have to read the source for it: an orphan's recorded path is matched exactly first; failing that, by unique basename. If two live repos share the basename, the orphan is reported as unmatched rather than guessed — wrongly merging two projects' memory would be worse than no match at all.

Recovered notes land **beside** existing ones, never merged into them; a name collision keeps both files, suffixing the orphan's copy with where it came from. Merging is the one operation that loses information without telling you, so it is never performed.

The scan behind this took 5m18s the first time it ran across a home directory. Skipping directories that never hold a bindable repo — `Library` alone was 17,189 of the 24,198 directories walked — brought it to 0.4s. The skip count is printed with the results, and `--no-prune` searches the skipped trees anyway; a faster scan that quietly ignored somewhere would be the same silent omission this tool exists to prevent. Orphans are also matched next to their own recorded paths first, so the wide walk usually never runs at all.

Originals stay on disk until you delete them, and deleting them is its own command:

```sh
commonbook prune --dry-run   # what would be deleted, what is kept, and why
commonbook prune             # asks, then deletes
```

`commonbook prune` is the only command that deletes anything, and it fails closed. It considers only stores carrying the marker `adopt` wrote, re-checks that every note in the store is present in the book it was adopted into, and keeps — naming the reason — anything it cannot verify, because those notes exist nowhere else. Piped or scheduled, with no terminal to ask on, it refuses entirely unless `--yes` is explicit; `adopt` holds itself to the same refusal before it copies.

---

## How it works

`autoMemoryDirectory` is a supported Claude Code setting, read from user, project, local and policy scope. Commonbook writes it into the repo's `.claude/settings.local.json`:

```json
{
  "autoMemoryDirectory": "/Users/you/.claude/commonbook/5bf8af88c69d4319/memory"
}
```

The id is `sha256` of the normalised git remote — `git@github.com:Acme/API.git`, `https://github.com/acme/api` and `ssh://git@github.com/acme/api/` all resolve to one identity. Because the setting lives inside the repo, it travels with the directory: the memory's location becomes configuration rather than something derived from a path that changes.

What that buys, stated precisely:

- **`mv` and rename stop mattering.** The path is no longer the key.
- **A fresh clone re-derives the same book.** Same remote, same id. The binding itself is a local, untracked file, so you run `commonbook bind` once per checkout — re-derivable, not automatic — and `commonbook doctor` in an unbound checkout says so.
- **Second clones share the book.** Claude Code already shares one store across the worktrees of a single checkout; the book extends that to a second clone in another directory, and to the same remote spelled over a different transport. Without it, a second clone gets a second, empty memory.

One honest caveat: the key can change too. Rename the org, or move the repo to another host, and the normalised remote — and so the id — changes. Existing checkouts keep working, because the binding is an absolute path, but a fresh clone would derive a new, empty book, and `commonbook doctor` in an old checkout reports the binding as pointing somewhere other than the freshly derived target. The fix is a rename, because books are plain directories: `mv` the book to the new id and re-run `commonbook bind`. This is the original bug one level up — much rarer, same shape — which is why doctor distinguishes *unbound* from *bound elsewhere* instead of collapsing both into one warning.

Books live under `~/.claude/commonbook/` by default. `commonbook bind --vault ~/notes` puts them under a directory of yours instead — a git repo, a synced folder, an Obsidian vault — and the notes become portable across machines.

Notes stay plain markdown in plain directories, and Commonbook is not in the read path. Delete the tool tomorrow and everything is still readable by anything.

---

## Commands

The fix is four commands:

| Command | What it does |
| --- | --- |
| `commonbook doctor` | What is bound, unbound, orphaned or at risk. Exits non-zero when something needs fixing. |
| `commonbook bind` | Point this repo's memory at the book keyed on its git remote. |
| `commonbook adopt` | Find orphaned stores, copy their notes into the right books, bind the repos. |
| `commonbook prune` | Delete originals verified present in a book. Keeps and names what it cannot verify. |

The rest exist because once notes survive, you accumulate more of them. None are required, and the fix does not change if you never run one:

| Command | What it does |
| --- | --- |
| `commonbook lint` | Check typed notes against the contract for their type. Exits non-zero on violations. |
| `commonbook aggregate` | One ranked, budgeted index across every book. Prints what it dropped. |
| `commonbook view` | One JSON document describing the machine's memory state. |
| `commonbook render` | That document as one self-contained HTML page. |
| `commonbook graph` | Whether an external code graph exists for this repo, and whether it is stale. |
| `commonbook autonomy` | Write-authority tiers for unattended work, and the invariants that check them. |
| `commonbook caps` | Machine-readable state, for skills and scripts to branch on. |

`bind`, `adopt` and `prune` all take `--dry-run`. The two that copy or delete refuse to run without a terminal unless `--yes` is passed.

---

## If you keep notes at scale

### Typed notes

The plugin ships three skills: **`recall`** searches the book before re-deriving something, and says "no prior note" rather than inventing history; **`capture`** writes one durable note; **`bind`** sets up or repairs the binding.

Two note types carry a contract, and `commonbook lint` enforces it. A *decision* must record the alternative that lost, or the question gets relitigated in six months. A *gotcha* must carry the error text verbatim in a fenced block, because retrieval is a literal string search and a tidied-up message is unfindable by the next person hitting it.

```console
$ commonbook lint

  decision     10
  gotcha       82
  reference    10

  x decision-move-auth-into-the-data-fetch.md   no 'Rejected' section — a decision without
                                                the alternative that lost is not a decision
  x gotcha-webview-onloadend-never-fires.md     no verbatim error text in a fenced block —
                                                retrieval is a literal string search
  …

11 violations
```

That is a real run over a real 102-note book. Untyped notes are ignored entirely — the contracts apply only to the types that have one.

### One index across every book

Auto memory is per repository; there is no supported way to ask what the machine knows about one topic across projects. Concatenating the books does not fit: on the machine this was written on, their indexes total 85 KB against a session-load budget of roughly 25 KB. So `commonbook aggregate` treats it as a ranking problem, not a merge problem. Pinned notes rank first, then notes whose topic appears in more than one book — cross-project knowledge is what the index exists to surface — then anything touched in the last 30 days, then newest first. Entries compete for a 25,000-byte, 200-line budget. The budget is a documented assumption, overridable by flag: the truncation behaviour was verified by reading the shipped binary (2.1.233), the exact limits could not be. On a real machine the result was 366 notes found, 151 indexed, 215 dropped — and the drop count is printed, because content past the cap is discarded at load time with no error, so an index that looks complete is not.

### The whole machine on one page

```sh
commonbook view | commonbook render && open commonbook.html
```

`view` needs no flags — it finds books by looking at where repos are already bound. `render` produces one HTML file with no JavaScript, no vendored library and no external requests: which repos are bound, which are one `mv` from losing their notes, what is already unreachable. Warnings are grouped by kind rather than listed flat — a real machine produced 109 of them, and a flat feed of 109 lines is wallpaper, not a finding. It is a state view, not a dashboard: no token counts, no cost charts. Those are answered better elsewhere.

### Code graphs

Commonbook does not build code graphs. If another tool does, `commonbook graph` answers the question graph tools do not answer about themselves — whether the graph still describes the code:

```console
$ commonbook graph status

  scope     ~/code/api-gateway  (repo)
  tool      /usr/local/bin/graphify
  graph     ~/code/api-gateway/graphify-out/graph.json  (3,176,897 B)
  size      2,692 nodes, 6,296 edges
  freshness STALE — built at ccda890104e0, HEAD is 14 commit(s) later
            structural answers may describe code that has changed
```

It validates the file against the schema it reads before trusting it, so an upstream format change becomes an error rather than a confidently wrong answer. It never installs anything, and an absent tool or absent graph is a normal state reported plainly, not a failure.

### Unattended writes

An agent that writes notes unattended does not fail by crashing. It fails by drift — duplication and confidently wrong notes accumulating while every run reports success. So write authority is explicit, and the default is the lowest tier:

| Tier | Name | What it permits |
| --- | --- | --- |
| 0 | `read-only` | Observe and report. No writes. **The default.** |
| 1 | `propose` | Write proposals to a review directory. Nothing lands until a human moves it. |
| 2 | `marked` | Write only inside generated markers, so a diff shows exactly what changed. |
| 3 | `full` | Unrestricted. Never implied by a lower tier working well, and refuses to be set without `--reason`. |

`commonbook autonomy` shows the tier, sets it deliberately, and — the part that matters — checks the invariants that make a tier true rather than nominal: proposals appearing under tier 0, proposals older than two weeks that no human has moved, more than 40 notes written in a day, tier 3 with no recorded reason, a config file that exists but does not parse. `check` exits non-zero on any of them.

---

## The rule underneath

Anything dropped, skipped or truncated is reported. An output that omits things while looking complete is worse than one that admits it is partial. That single rule is why the scan prints how many directories it skipped, why `adopt` lists what it could not match instead of guessing, why `prune` names every store it keeps and the reason, why `aggregate` prints its drop count, and why the graph adapter turns schema drift into an error instead of an answer.

---

## Non-goals

- **Owning your notes.** Notes are markdown files in ordinary directories, readable and editable by anything. Commonbook is not in the read path, and deleting it loses nothing.
- **Migrating sessions.** Conversation history and todo state are keyed on the path the same way and stay orphaned; Commonbook moves memory only. For a one-time migration of everything else, see Related work.
- **Reducing consumption.** It reduces loss. The session-load budget stays whatever Claude Code makes it; `aggregate` exists to spend that budget deliberately, not to raise it.
- **Existing forever.** `autoMemoryDirectory` is a supported setting, and a first-party remap from Anthropic would make `bind` unnecessary. That would be the right outcome — `adopt` would still be needed for notes orphaned before it shipped.

---

## Related work

[claude-repath](https://github.com/xPeiPeix/claude-repath) approaches the same bug from the other side: a one-time migration of everything — sessions, todos, `~/.claude.json`, worktree configuration — run when you move a project and can name the old and new paths. Commonbook is a standing remap of memory only: nothing to remember at move time and nothing to re-run, but it does not carry session history. They compose — repath to move what already exists, Commonbook so memory never needs moving again.

---

## FAQ

**Does this replace Claude Code's memory?**
No. It redirects where the built-in auto memory writes. Claude still writes the notes; Commonbook decides the address.

**Does it work without a git remote?**
Yes, with a warning. No remote falls back to the first-commit hash, which is stable across moves. No git at all falls back to the path — the same weakness this tool exists to fix, so `commonbook doctor` says so loudly instead of pretending the binding is safe.

**What if `.claude/settings.local.json` is tracked in git?**
Commonbook sets `skip-worktree` and verifies it took, so your local absolute path is never committed; if that fails, it refuses to bind rather than leak the path into everyone's clone. Note that Claude Code treats a tracked settings file as repository-supplied and holds its rules until you trust the folder, so on such a checkout the binding takes effect after that prompt.

---

## Credits

MIT licensed.

Related methodology, neither bundled here — install them directly if you want them:

- [obra/superpowers](https://github.com/obra/superpowers) — a broad skills methodology (MIT)
- [rebelytics/one-skill-to-rule-them-all](https://github.com/rebelytics/one-skill-to-rule-them-all) — task-observer, a meta-skill that watches sessions and proposes improvements to your other skills, by Eoghan Henn ([rebelytics.com](https://rebelytics.com)), CC BY 4.0
