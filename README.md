# Commonbook

**Claude Code forgets a project when you move or rename its folder. This fixes that, and recovers the notes you already lost.**

Claude Code's auto memory is stored in a directory named after your project's **filesystem path**. Rename the folder, move it into a subdirectory, reorganise your workspace — and a fresh, empty memory appears. The old notes are still on disk. Nothing can reach them again.

There is no built-in remap. Commonbook keys the memory on the repo's **git remote** instead, so it survives moves, renames and fresh clones.

→ **[How this bug works, and how to check your own machine](docs/the-bug.md)**

```console
$ commonbook doctor

  repo      /Users/you/code/api-gateway
  identity  remote  github.com/acme/api-gateway
  binding   unbound — memory is keyed on this path and will
            orphan if the directory moves. Fix: commonbook bind

  stores    14 with notes, 3 orphaned
            26 notes, 41,208 bytes unreachable — commonbook adopt
```

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
curl -fsSL https://raw.githubusercontent.com/erfanhabibipanah/commonbook/main/plugins/commonbook/bin/commonbook.py -o ~/.local/bin/commonbook
chmod +x ~/.local/bin/commonbook
```

Requires Python 3.9+ and git. Works on macOS, Linux and WSL.

---

## Recover memory you already lost

```console
$ commonbook adopt --dry-run

found 3 orphaned memory store(s)
  searched /Users/you to depth 5 · 22 live repo(s)

  api-gateway         11 notes   18,400 B  /Users/you/dev/api-gateway
  billing-worker       9 notes   14,902 B  /Users/you/old-projects/billing-worker
  unmatched            6 notes    7,906 B  /Users/you/scratch/spike

dry run — would adopt 2 store(s), 33,302 bytes, 2 project(s)
```

`adopt` matches each orphan to a live repo, copies the notes into that project's book, and binds the repo so it cannot happen again.

Recovered notes land **beside** existing ones, never merged into them. Merging is the one operation that loses information without telling you.

Originals stay on disk until you remove them:

```sh
commonbook prune --dry-run   # verify every note is safely in the book
commonbook prune             # then delete the originals
```

`prune` deletes an original **only** after confirming each of its notes exists in the book. Otherwise it keeps the store and says why — those notes exist nowhere else.

---

## Commands

| Command | What it does |
| --- | --- |
| `commonbook doctor` | What is bound, unbound, orphaned or at risk. Exits non-zero when something needs fixing. |
| `commonbook bind` | Point this repo's memory at a book keyed on its git remote. |
| `commonbook adopt` | Find orphaned memory, copy it into the right book, bind the repo. |
| `commonbook prune` | Delete originals that are verified present in a book. |
| `commonbook lint` | Check notes against the contract for their type. Exits non-zero on violations. |
| `commonbook graph` | Report whether a code graph exists for this repo, and whether it is stale. |
| `commonbook caps` | Machine-readable state, for skills and scripts. |

Every destructive or writing command takes `--dry-run`.

---

## How it works

Claude Code reads `autoMemoryDirectory` from settings. Commonbook writes it into the repo's `.claude/settings.local.json`:

```json
{
  "autoMemoryDirectory": "/Users/you/.claude/commonbook/5bf8af88c69d4319/memory"
}
```

That id is `sha256` of the normalised git remote. Because the setting lives **inside the repo**, it travels with the directory — the memory's location becomes configuration rather than something derived from a path that changes.

Three properties the default does not have:

- **Survives `mv` and rename.** The path is no longer the key.
- **Re-derivable after a fresh clone.** Same remote, same id.
- **One book per repo, not per checkout.** Every worktree and second clone share it, because remotes are normalised: `git@github.com:Acme/API.git`, `https://github.com/acme/api` and `ssh://git@github.com/acme/api/` all resolve to one identity.

Notes stay plain markdown in a plain directory. Commonbook is not in the read path — delete it tomorrow and everything is still readable.

### Keeping books in sync or version control

```sh
commonbook bind --vault ~/notes
```

Books live under that directory instead. Point it at a git repo, a synced folder or an Obsidian vault and the notes become portable across machines. Without it they are local to this one.

---

## Skills

The plugin ships three, deliberately:

- **`recall`** — search the book before re-deriving something. Says "no prior note" rather than inventing history.
- **`capture`** — write one durable note. A *decision* must record the alternative that lost; a *gotcha* must carry the error text verbatim, because retrieval is a literal string search.
- **`bind`** — set up or repair the binding.

The typed requirements are the point. A decision without its rejected alternative gets relitigated in six months; a gotcha with a tidied-up error message cannot be found by the person hitting it next.

`commonbook lint` enforces them across a whole book:

```console
$ commonbook lint

  decision     10
  gotcha       82
  reference    10

  x decision-move-auth-into-the-data-fetch.md   no 'Rejected' section — a decision without
                                               the alternative that lost is not a decision
  x gotcha-webview-onloadend-never-fires.md     no verbatim error text in a fenced block —
                                               retrieval is a literal string search

2 violation(s)
```

It ignores untyped notes entirely — the contracts apply only to the two types that have one.

---

## Code graphs

Commonbook does not build code graphs. If you use a tool that does, `commonbook graph status`
answers the question those tools do not answer about themselves — whether the graph still
describes the code:

```console
$ commonbook graph status

  scope     ~/code/api-gateway  (repo)
  tool      /usr/local/bin/graphify
  graph     ~/code/api-gateway/graphify-out/graph.json  (3,176,897 B)
  size      2,692 nodes, 6,296 edges
  freshness STALE — built at ccda890104e0, HEAD is 14 commit(s) later
            structural answers may describe code that has changed
```

It never installs anything, and it validates the file against the schema it reads before trusting
it — an upstream format change becomes an error rather than a confidently wrong answer.

A `.commonbook/graph-scope` marker widens the scope to a directory holding several repos that share
an interface. The search is bounded, so it can never expand to a whole workspace, where one graph
would be meaningless: unrelated repos share no imports, so clustering just returns the directory
names back.

---

## Running it unattended

If you schedule anything that writes notes, the question is not whether it works — it is what
happens over months when it is subtly wrong. Drift, duplication and confidently-wrong notes do not
announce themselves; the run reports success while the corpus gets worse.

So write authority is explicit and graduated, and the default is the lowest:

| Tier | Name | What it permits |
| --- | --- | --- |
| 0 | `read-only` | Observe and report. No writes at all. **The default.** |
| 1 | `propose` | Write proposals to a review directory. Nothing lands until you move it. |
| 2 | `marked` | Write, but only inside generated markers, so a diff shows exactly what changed. |
| 3 | `full` | Unrestricted. Never implied by a lower tier working well. |

```sh
commonbook autonomy show          # current tier and what it permits
commonbook autonomy set 1         # raise it deliberately
commonbook autonomy check         # assert the invariants; non-zero on failure
```

Tier 3 refuses to be set without `--reason`, because that sentence is the only explanation anyone
gets six months later.

`check` is the part that matters. A tier is a claim; the invariants are what make it true:

- a tier-0 repo with proposals sitting in the review directory — something wrote, so the tier is not
  describing reality
- proposals older than two weeks — the gate is nominal, because the safety property of tier 1 is a
  human and an untouched queue says there is none
- more than 40 notes written in a day — authorship has a human rhythm and a loop does not
- tier 3 with no recorded reason
- a config file that exists but does not parse, which silently means tier 0 while the operator
  believes otherwise

---

## FAQ

**Why did Claude Code forget my project?**
Its memory directory is named after the project's path. Rename or move the folder and it looks in a location that has never been written to. The old notes still exist under `~/.claude/projects/`, orphaned.

**Does this replace Claude Code's memory?**
No. It redirects where the built-in memory writes. Claude still writes the notes; Commonbook decides the address.

**Does it work without a git remote?**
Yes, with a warning. No remote falls back to the first-commit hash, which is stable. No git at all falls back to the path — which has the same weakness this tool exists to fix, so `doctor` says so loudly.

**Will it touch my existing notes?**
It never edits, merges or deletes a note during `bind` or `adopt`. `prune` is the only command that deletes, only for stores it has verified, and it refuses when it cannot.

**Is anything sent anywhere?**
No. There is no network call in the tool at all.

**What if my `settings.local.json` is committed to git?**
Commonbook detects it and sets `skip-worktree` so your local path is never committed. If that fails, it refuses to bind rather than leak an absolute path into everyone else's clone.

---

## Credits

`commonbook` is MIT licensed.

Related work worth knowing:

- [obra/superpowers](https://github.com/obra/superpowers) — a broad skills methodology (MIT)
- [rebelytics/one-skill-to-rule-them-all](https://github.com/rebelytics/one-skill-to-rule-them-all) — task-observer, a meta-skill that watches sessions and proposes improvements to your other skills, by Eoghan Henn ([rebelytics.com](https://rebelytics.com)), CC BY 4.0

Neither is bundled here — install them directly if you want them.
