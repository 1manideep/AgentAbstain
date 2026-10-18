# Practice codebook

The written codebook that MEMORY_EVOLUTION section 7.3 asks for: how to code the practices in agents' memory policy files (`/memories/memory_policy.md`, one row per version in `policy_versions`). It serves three coders:

- a human coder, for the grounded-theory pass over a random sample of 200 policy versions (7.3 step 1) and for the 10 to 20% validation sample (step 3), blind to arm (step 4);
- an LLM coder for the rest (step 2). It is paid and is not built yet; this codebook is its prompt material;
- the deterministic keyword coder in `void/research/outcomes/practices.py` (`keyword_code`), a free baseline that also codes runs no one has coded by hand, for the `cluster_entropy` outcome.

The categories below are the starting scheme of 7.3 and are expected to change after the first read of real policies [assumption]. When they change, change them here, in `CODE_NAMES` and `KEYWORDS` in `void/research/outcomes/practices.py`, and in the 7.3 table together: `tests/test_outcomes_transmission.py` checks that the three agree, and that every example below gets the stated code from the keyword coder.

## Unit of coding

- One **practice line** of one policy version: a non-empty line that is not a markdown heading (`void.culture.practices.policy_lines`). A version of five bullets is five items. Each item gets exactly one code.
- The coder sees the other lines of the same version (rows with the same `policy_id`, in `line_no` order) and may use them to read a line, but codes each line on its own.
- The coder never sees the arm, experiment, run, agent or day. The sample file has no such column and its ids are salted hashes; the key that maps items back to runs is a separate file the coder is not given.

## Decision rules

1. Code the line's purpose: what the practice is for, not which words it happens to use.
2. A line that does two things gets the code of its main action. `Delete any note older than two days unless it names a node or a person.` is T4: the action is deletion, the exception refines it.
3. A line that is not a memory practice at all (a link left in the policy such as `[[Node n3]]`, a fact, a note title, a motto) is T9.
4. When two codes fit equally well, take the earlier one in the order T5, T8, T6, T4, T1, T2, T3, T7: the narrower practice before the broader one. The keyword coder breaks its ties in the same order, so the two coders differ on purpose, not on tie-breaking.
5. Use T9 only for lines that fit no category. A practice that fits a category loosely gets that category.

## Categories

The nine codes of MEMORY_EVOLUTION 7.3, with their names exactly as the 7.3 table gives them.

### T1 Indexing: maps of content, hub notes

Practices that build structure for finding notes again: index notes, maps of content, one hub note per place or person that other notes hang off, links between notes, tables of contents.
Not T1: tagging a note with its kind (T3); merging several notes into one (T2).

Examples:
- `Link every note to the node or agent it is about, like [[Node n3]] or [[Ada]].`
- `Maintain an index note that links to every other note with one line each.`
- `Keep one index note per node listing what I know about it. Update it, do not duplicate it.`
- `Hub notes: one per region, linking its nodes.`

Keywords: `index`, `hub`, `map of`, `maps of`, `table of contents`, `link`, `backlink`, `overview`, `catalog`, `directory`

### T2 Compression: summaries, merging notes

Practices that make the vault smaller or denser without throwing information away on purpose: writing summaries or digests, merging notes on the same subject, keeping one note where there were several, distilling raw episodes into a condensed form.
Not T2: deleting notes outright (T4); an index that points at notes without condensing them (T1).

Examples:
- `Merge notes about the same node into one note.`
- `Condense old notes into one digest per week.`

Keywords: `summar`, `merg`, `compress`, `condens`, `consolidat`, `combin`, `digest`, `duplicat`, `dedup`, `distil`

### T3 Typing and tagging: fact, claim, plan

Practices that classify notes: tags, types, labels, a field saying whether a note holds a fact, a claim or a plan, a fixed note format by kind.
Not T3: recording who a claim came from (T5); linking notes (T1).

Examples:
- `Tag every note as fact, claim or plan.`
- `Label each note with the kind of fact it holds.`
- `At the end of each day, write one note per important fact: who, what, where, when.`

Keywords: `tag`, `type`, `label`, `categor`, `classif`, `fact`, `claim`, `plan`, `frontmatter`, `front matter`

### T4 Pruning: deleting, expiring, correcting

Practices that remove or repair what is stored: deleting notes, expiring them after a time, dropping stale or wrong ones, correcting a note that turned out wrong.
Not T4: condensing notes into a summary (T2); marking a claim unverified without changing it (T5).

Examples:
- `Delete notes that turned out to be wrong.`
- `Keep the last five; delete older ones.`
- `Delete any note older than two days unless it names a node or a person.`
- `Expire notes about node richness after one day.`
- `Fix a note as soon as it turns out wrong.`

Keywords: `delet`, `remov`, `prun`, `expir`, `forget`, `discard`, `drop`, `archiv`, `older than`, `outdat`, `stale`, `obsolet`, `wrong`, `correct`, `fix`, `clean up`, `cleanup`

### T5 Source tracking: who said it, and whether it proved true

Practices that keep provenance: who said or wrote something, where a note came from, how many sources back it, whether it later proved true or false.
Not T5: deciding whom to trust or talk to (T8), unless the line is about recording the evidence.

Examples:
- `If two notes disagree, keep the newer one and write down where each came from.`
- `One note per agent: what they told me and whether it turned out true.`
- `For every fact I write, record who said it and when.`
- `A fact from one source is a rumour; from two it is knowledge.`
- `Mark a note as a rumour until a second agent confirms it.`

Keywords: `source`, `who said`, `who told`, `told me`, `came from`, `heard from`, `provenance`, `rumour`, `rumor`, `verif`, `turned out true`, `turned out to be true`, `proved true`, `confirm`, `cite`, `attribut`

### T6 Question anticipation: writing notes as answers to future needs

Practices that write for a later reader: notes phrased as answers to questions the agent expects to face, lists of what it will need to know, gaps it wishes it had filled.
Not T6: a rule for what to do next (T7).

Examples:
- `Each night write down one thing I wish I had known today, and where I could learn it.`
- `Write each note as the answer to a question I will need to ask later.`

Keywords: `question`, `wish i had known`, `wish i knew`, `need to know`, `will need`, `might need`, `future`, `anticipat`, `answer`, `look up`, `lookup`

### T7 Heuristics: rules of thumb for action

Practices that turn memory into action: rules of thumb about where to go, what to forage, which route to repeat, what to spend; a lessons note of such rules; a ledger of what paid.
Not T7: rules about the notes themselves (T1 to T6); rules about other agents (T8).

Examples:
- `Before moving, read my node notes and go to the richest one I have not visited today.`
- `Repeat the best route; change one step when it stops paying.`
- `Keep a single note called Lessons with short rules I have learned.`
- `Never forage at a node that paid nothing twice in a row.`
- `Prefer nodes I have not visited in two days.`

Keywords: `rule`, `heuristic`, `lesson`, `strategy`, `tactic`, `before moving`, `go to`, `move to`, `route`, `forag`, `richest`, `best`, `avoid`, `prefer`, `repeat`, `income`, `spend`, `earn`, `pay`, `paid`

### T8 Social: whom to trust, what to share

Practices about other agents: whom to trust or avoid, liars, allies, what to tell or share with neighbours, gossip.
Not T8: recording who said a thing (T5).

Examples:
- `Trust agents whose claims were right; ignore the rest.`
- `Do not trust anyone who lied to me once.`
- `Tell neighbours about empty nodes.`
- `Write notes only about dangers: empty nodes, liars, bad weather.`

Keywords: `trust`, `distrust`, `mistrust`, `share`, `liar`, `honest`, `gossip`, `neighbour`, `neighbor`, `ally`, `allies`, `friend`, `deceiv`, `decept`, `betray`, `cooperat`, `whom`, `people`, `person`

### T9 Other

A line that fits none of T1 to T8, or is not a practice (rule 3). A new kind of practice that keeps turning up here is a candidate for a new category at the next revision.

Examples:
- `Everything else I can see for myself.`
- `Keep notes short.`
- `Write the day number at the top of every note.`
- `[[Node n3]]`

Keywords: none (T9 is what the keyword coder gives when no rule matches)

## Hard cases

Lines where the definitions above and the keyword coder disagree. The middle column is this codebook's reading of the definitions [assumption]; the right column is what `keyword_code` returns (checked by the test).

| Line | Codebook | Keyword coder |
|---|---|---|
| `Edit it; never add a second lessons note.` | T2 | T7 |
| `Rebuild it nightly.` (after an index line) | T1 | T9 |
| `One note per node: where it is, how rich it was, when I last saw it.` | T1 | T9 |
| `Each night, one note: income, spend, what worked, what failed.` | T2 | T7 |
| `Each night write a summary of the day and delete the raw notes it covers.` | T2 | T4 |
| `Share my best route only with agents who shared theirs.` | T8 | T7 |

## The keyword coder

`keyword_code(line)` in `void/research/outcomes/practices.py`:

1. Normalize the line: lower case, every run of characters that are not letters or digits becomes one space (`normalize_line`).
2. A keyword rule matches when its words start words of the line, in order: `delet` matches "deleted", `map of` matches "map of nodes", and nothing matches inside a word (`link` does not match "backlink", which has its own rule).
3. Score each code by the number of its rules that match. The highest score wins; a tie goes to the earlier code in T5, T8, T6, T4, T1, T2, T3, T7. No match gives T9.

It is a pure function of the line: the same line always gets the same code, in any run. It reads words, not purpose, so it errs where a line's purpose is not in its words (the hard cases above) [assumption: how often that happens in evolved policies is unknown until a human-coded sample is compared with it by kappa].

## Workflow

```bash
# 1. a blind sample of 200 policy versions from every run under runs/ (one row per practice line)
.venv/bin/python scripts/code_practices.py sample runs --n 200 --out sample.csv --salt <private>
#    -> sample.csv (item_id, policy_id, line_no, text, code) for the coder; sample.key.csv maps items to runs
# 2. the human coder fills the code column of a copy of sample.csv (human.csv) with T1 to T9
# 3. the keyword coder codes the same items
.venv/bin/python scripts/code_practices.py code sample.csv --coder keyword --out keyword.csv
# 4. agreement between the two
.venv/bin/python scripts/code_practices.py kappa human.csv keyword.csv
```

The sampler draws versions, not lines, and by default each distinct policy text once, so the seed text and verbatim inherited copies do not crowd the sample (`--all-versions` keeps them). Versions come in a seeded random order with their lines together. The default salt is derived from the seed; pass a private `--salt` when the coder could know how the runs are laid out.

A coded file with `text` and `code` columns copied into a run directory as `practice_codes.csv` is what the `cluster_entropy` outcome uses for that run, line by line (a line the file does not cover falls back to the keyword coder).

## Agreement

Cohen's kappa over the items both coders coded: `kappa = (p_o - p_e) / (1 - p_e)`, with `p_o` the share of items given the same code and `p_e` the agreement expected by chance from each coder's code shares. Perfect agreement is 1.0 (also when both coders used one code for everything, where the formula is 0/0). The implementation is checked on a textbook 2 by 2 table (50 items: 20 yes-yes, 5 yes-no, 10 no-yes, 15 no-no; `p_o` 0.7, `p_e` 0.5, kappa 0.4) in `tests/test_outcomes_transmission.py`.

7.3 plans to report kappa on a human-coded 10 to 20% sample; the threshold for an acceptable LLM coder is not fixed yet [assumption].

## Not coded here yet

7.3 also asks whether a practice is a known personal-knowledge-management method (index notes, Zettelkasten-style links, PARA-style buckets) or something outside that vocabulary, and three further axes (which memory component a change touches, rules or examples, raw or condensed). They are not in this codebook yet; they belong in the revision after the first read of real policies.
