# Answers in the dialogue, the page optional

Status: built, 2026-11-26. `ask` and `answer` for trips and series; `said` is kept per answer and in the plan's proof. `via` is read and not written: the `said` rows say which answers came from a conversation.

## Why

`AGENTS.md` exists so that the whole tool can be driven from a conversation with an agent.
Two steps break that: `trips review` and `series review` say "hand the page to the owner and
stop", and the answers file is something only the page writes. The owner has to open a browser,
click through a list, save a file and come back. On 2026-11-26 they asked why the HTML pages
appeared at all, and asked for a questionnaire in the chat. That worked: three questions, a plan
and an apply in minutes (Costa Rica, trip 555012).

The page was never what made the answers honest. Three rules did:

1. nothing is preselected or recommended,
2. a word that releases a run is the owner's own text, copied verbatim,
3. a blank is not "no", and "I don't remember" is final.

A chat can follow all three. What the page added beyond them was a format check and a record
of what was shown. Both can move into the CLI.

## What is already true in the code

- `trips plan` and `series plan` read an answers **file** and do not care who wrote it.
  `trip_list.read_answers` checks every field, id, quality, word and focus.
- The page is only a renderer of the list, and the list is hashed and bound to the answers.
- So the gap is in `AGENTS.md`, which forbids the agent from writing that file, and in the
  absence of a text version of the page.

## Decision

The dialogue is the default way to answer. The page stays, as an option for a long list or
when the owner asks for it.

### 1. `wanderfill trips ask LIST [--answers FILE] [--json]`

This is the page as text. It prints what still takes an answer, by group in time order. A group
is a stretch as `group()` makes it with no cut answered, so a stay that may not be over starts one.
For each run it shows the id, region, days, photo count, first and last photo with the clock,
evidence, flags, reasons, the region's visits, what the run takes, whether words are needed, and
any flight or jump days. A run stays on the list while `missing()` says something about it: it has
no answer, or it has a quality on a held run with no words where words are needed. `no` and `?`
are final. Every cut that is not forced and that the list shows is asked, including the one before
an open stay (`between_groups`): blank there is a new trip while that stay has no words, and the
same trip once it has them. The text output names the cuts that have a suggestion or lie between
groups, and counts the rest. `--json` gives all of it, plus `complete` and `not_read`. Both exit 3
on an incomplete list. It writes nothing.

`series ask LIST [--series ID] [--answers FILE] [--json]` does the same for a series list.

### 2. `wanderfill trips answer LIST ANSWERS --said TEXT [--run ID=Q] [--words ID=TEXT] [--out ID=DAY] [--in ID=DAY] [--cut ID=cut|same] [--replace]`

This records one owner reply. `--said` is required and holds the owner's reply verbatim. It is kept
in the file's `said` list, with the ids it answered, so one reply such as "Aldmoor 3, Berrow 2" can
give several answers. Every reply is kept, in the order given.

- The values are `ID=VALUE`, split at the first `=`, so words may contain `=`. An id given twice
  in one call is refused. `--out` and `--in` days are sorted into date order.
- Only the fields given change. A field that already holds another value is refused unless
  `--replace` is given, which records a change of mind. The other fields of the row are kept.
- One writer at a time, through a lock file. The draft is a temporary file of its own in the
  same directory, checked by `read_answers` before it replaces the answers file and removed on
  any failure. Errors name the answers file, not the draft.
- The plan's proof of each trip carries, once each, the replies of its runs, of the runs left out
  between them, and of every cut from the one before the trip to the one after it.

`series answer LIST ANSWERS --said TEXT --item SERIES:ITEM=y|n|q [--replace]` works the same way.
A tick's proof carries the replies to its item. Replies that were only `n` or `q` stay in the
answers file, whose hash is among the plan's sources.

The answers format stays 1, with optional `said` and `via` fields. `via` is read and never
written.

### 3. What the agent does (`AGENTS.md` §5 step 2, §6 series step 2)

This replaces "Hand the page to the owner and stop":

> Run `ask` and put the questions to the owner in the chat, one trip, or a few runs, at a time,
> with the facts `ask` printed and no others. Every option is equal: no "(Recommended)", no
> order but 1–6, no, don't remember; nothing is assumed. A question tool may be used on
> those terms. Copy their reply into `answer --said` as written; if it does not say
> which answer goes with which run, ask again rather than match it yourself. Words that
> release a run are what they typed, not a summary of it and not your sentence they agreed
> to. Blank stays blank. "Не помню" is final. Never ask leading questions (rule 7 stands
> as it is).
>
> More than ~20 questions: say how many, and let the owner choose between a narrower
> `--since`/`--focus`, several sittings, or the page.

Delete the line "the agent may not write the answers file itself", along with the memory note
about the classifier. The agent writes the file only through `answer`.

### 4. What does not change

Rules 0–7, the hold reasons and what releases them, one plan per kind with one approval,
`check` before `apply`, and verify. The page stays as it is, the same file is behind both, and an
owner should not save the page over a file `answer` wrote: the page reads no answers file.

## Tests

- `ask` prints only what takes an answer, nothing preselected, and no item that has no photo
  (series). `--json` matches the text.
- `answer` refuses without `--said`, refuses words where none are taken, refuses a quality
  where only `no` is taken, refuses an id that is not shown (focus), and refuses overwriting
  without `--replace`. It writes atomically, and what it writes loads through `read_answers`.
- One `--said` covering three runs is stored once per answer. The plan carries it in `memory`
  proof verbatim.
- Format 1 answers still plan the same way.

## Docs

The README gets "Answering in a conversation" before the page section, and the page is described
as the alternative. `AGENTS.md` §5 and §6 change as in 3. The module table gains the CLI part.
