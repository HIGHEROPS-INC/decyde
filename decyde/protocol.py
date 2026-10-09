"""The instructions `decyde setup` writes into each agent's global instruction file."""
from __future__ import annotations

BEGIN, END = "<!-- decyde:begin -->", "<!-- decyde:end -->"

TEMPLATE = """## decyde (routing asks for {name} through an inbox)

decyde is **off** by default. It turns **on** for the rest of a session when {name}
says "use decyde" or "decyde on" (or runs `/decyde on`), and off again on "decyde off".
While it is off, ask in chat as usual; the only part that still applies is acting on
`[decyde]` answers to questions you already posted.

{name} runs many agents at once, often unattended, and misses asks that are buried in
terminal output. While decyde is on, **every ask goes through `decyde ask`**, not only
into chat. An ask is anything that needs {name} before it happens:

- **A decision**: product, design, scope, naming, priorities, a trade-off between options.
- **A confirmation you will not proceed without**: anything irreversible or destructive
  (merges, deletes, migrations, force pushes), production or client data, deploys,
  sending email or messages, spending money.
- **Something only {name} can do**: log in, provide a key or secret, approve in a
  dashboard, buy something, any offline step.
- **A hand-off at the end of your turn**: "your call", "want me to...?", "should I...?",
  "let me know", "I'll leave it to you", "tell me to run it and I'll watch it".

Not asks: your harness's own tool-permission prompts, status updates that need nothing
from {name}, and questions you can answer from the code, docs or sensible defaults.

Example. Ending a turn with "Your call: the five-company group still has 3 unmerged
duplicates. Merges can't be undone, so I'll leave it to you, or tell me to run it" is an
ask. Post it (title "Re-run the five-company merge?", options "Run it, I'll watch" and
"I'll do it myself", your recommendation), then end with "Waiting on decyde #N".

The rule: if your message would end with a question for {name}, or with something
{name} must do or approve, post it to decyde first. One question per decision.

**Ask** (always identify yourself and what you are working on):

```bash
decyde ask --agent claude|codex|grok|antigravity|gemini|other \\
  --name "<model + your role, e.g. 'Codex, billing lane'>" \\
  --task "<what you are working on right now>" \\
  --title "<one-line summary of the decision>" \\
  --question "<the full question>" \\
  --context "<what {name} needs to know to decide>" \\
  --option "<choice A>" --option "<choice B>" \\
  --recommend "<your pick and why>" \\
  --urgency high    # only if you are fully blocked; otherwise omit (normal) or use low
```

It prints a question number, e.g. `#12`. Run it from your project directory so the
project, git branch and terminal pane are captured automatically.

**While you wait:** keep working on anything the decision does not block. Do not guess
the answer to the blocked part, and do not do the thing you asked about.

**Getting the answer:**
- The answer may arrive on its own as a message starting with `[decyde]` (typed into
  your Herdr or tmux pane, or handed back by an agent hook).
- Also check back yourself: `decyde check 12` exits 0 and prints `ANSWER:` when
  answered, 2 while still open, 3 if dismissed. Check at natural breakpoints and before
  you finish your turn.
- If you have nothing else to do, block on it: `decyde wait 12 --timeout 900`.
  Otherwise keep each command under a couple of minutes while a question is open: an
  answer handed over between tool calls waits for a running one.
- When you have the answer, run `decyde ack 12` and act on it.
- If the question stops mattering: `decyde cancel 12 --reason "..."`.

End a turn with an open question by naming it: "Waiting on decyde #12: <title>"."""


def block(name: str) -> str:
    return f"{BEGIN}\n{TEMPLATE.format(name=name)}\n{END}"


def upsert(text: str, name: str) -> str:
    """Insert or replace the decyde block in an instruction file's text."""
    new = block(name)
    if BEGIN in text and END in text:
        head, rest = text.split(BEGIN, 1)
        return head + new + rest.split(END, 1)[1]
    return (text.rstrip("\n") + "\n\n" if text.strip() else "") + new + "\n"


def remove(text: str) -> str:
    if BEGIN not in text or END not in text:
        return text
    head, rest = text.split(BEGIN, 1)
    return head.rstrip("\n") + "\n" + rest.split(END, 1)[1].lstrip("\n")
