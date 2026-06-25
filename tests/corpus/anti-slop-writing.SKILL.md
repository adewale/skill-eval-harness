---
name: anti-slop-writing
description: Use this skill to review, tighten, draft, and rewrite prose so it does not read like generic LLM output, AI writing, bland marketing, or generic launch copy. Apply it when the user asks to make writing less generic/AI-sounding, tighten a talk intro, sharpen presentation/slide copy, or improve articles, wiki pages, README text, emails, posts, scripts, product/DevRel copy, launch copy, and other important writing with AI tropes, inflated significance language, generic cadence, weak flow, or marketing fog.
license: MIT
compatibility: Agent Skills clients including Codex, OpenCode, Pi, Gemini CLI, and Claude Code.
---

# Anti-Slop Writing

Use this skill for any important writing or rewriting task.

Load local references only when the task needs them:

```txt
references/anti-slop-writing-doctrine.md — read for a full prose review or when changing the doctrine.
references/flow-by-relation.md — read when paragraphs are locally clear but list-like, or when fixing transitions/conclusions.
references/rewrite-patterns.md — read when producing multiple concrete rewrites or adding a new failure pattern.
```

## Core principle

```txt
Sharp detail beats inflated significance.
```

AI writing often fails by doing this:

```txt
less detail, more importance
```

Fix by doing this:

```txt
more detail, earned importance
```

## Before writing

Answer:

```txt
What is the exact point?
What concrete detail proves it?
What sentence machinery is making it feel true?
What should the reader remember?
```

If those are unclear, ask or inspect sources before drafting.

## Source-backed detectors

Use these checks before rewriting:

```txt
AI-writing signs: superficial analysis, undue significance language, canned emphasis, negative parallelisms, rule-of-three overuse, formulaic dashes, table/bold formatting as fake structure.
Rhetorical-style drift: watch noun-heavy abstractions, nominalizations, phrasal coordination, and informational density without mechanism.
Practical AI-editing tells: repeated “Not X. Y.” rhythm, symmetrical paragraph length, parallel headings, and bullet + bold-header + colon patterns.
Copula displacement: watch “serves as,” “stands as,” “features,” “marks,” and “represents” replacing plain “is/are.” Keep the verb when it does concrete work (enumerating, defining, locating); replace it when it only inflates a copula.
Hedged symmetry: watch “Whether you’re X or Y,” “While X, Y is also important,” and similar templates that address every reader or every value at once. Keep the structure only when it names a real branching condition with distinct downstream behavior.
Em-dash cadence: do not ban em-dashes; watch decorative clusters that use dashes for emphasis instead of for parenthetical or appositive insertion. Reduce count; keep dash insertions/pairs that bracket inline definitions or genuine asides.
Rhetorical staccato: do not ban antithesis/parallelism; watch antithetical parataxis where rhythm implies the relation before evidence is unpacked.
Hypotaxis preference: when the relation matters, prefer subordination and connective syntax over side-by-side clauses; use “because,” “although,” “when,” “while,” “where,” “once,” or an explicit summary noun to show which idea modifies which.
Flow-by-relation test: paragraphs should make the next question possible, not merely sit beside each other in a plausible order. At section boundaries, name the relation: cause, contrast, dependency, inference, resolution, scope change, or level-of-detail change.
Conclusion test: a conclusion should return to the concrete carrier, admit the limit if needed, and state the reusable structure. Avoid ending with a generic thesis sentence that could belong to any essay in the category. Reject outline templates such as “Despite challenges, X continues to thrive” and “Looking ahead, X will play an increasingly pivotal role” unless backed by a specific challenge or named next step.
Unseeing frame: unsee the polished sentence into the machinery that made it feel true; inspect cadence, contrast, omitted relation, evidence scope, and abstraction leaks.
Emphasis-source test: ask whether the emphasis comes from the idea or from a borrowed pattern. To apply, write the flattened version of the line in your critique (cadence removed, claim preserved) and judge whether the residual claim still names an actor, mechanism, or limit. If it collapses into a generic statement, the rhythm was carrying it; if it still lands, the idea was.
Syntax-relation test: ask whether the syntax clarifies the relation between ideas or only suggests one. To apply, restate the implied relation in plain prose using a connective (because, although, when, where, once, so that). If you cannot supply the connective without inventing it, the syntax was standing in for a relation that was not there.
Contribution bar: contribution must justify length; add judgment, framing, depth, or cut.
DevRel pressure test: durable prose needs judgment, evidence, runnable artifacts, and taste.
```

## Default editing pass

Before finalizing prose:

```txt
1. Delete generic opening.
2. Find prestige vocabulary clusters.
3. Replace abstract nouns with concrete mechanisms.
4. Remove “not just X but Y” unless it is the exact point.
5. Cut superficial “highlighting/underscoring” clauses.
6. Check every claim of importance against evidence.
7. Replace vague actors with named sources, named uncertainty, or an `ask-author` note that says what to ask. Do not invent a name, count, tool, or timing to fill a `Concrete rewrite` slot.
8. Collapse redundant bullets.
9. Vary sentence rhythm deliberately.
10. Audit staccato contrast cadence by identifying the hidden relation: contrast, evidence scope, cause, consequence, exception, or scope control.
11. Prefer hypotaxis to parataxis when the relation matters: subordinate the secondary idea instead of placing equal-weight clauses side by side.
12. Check paragraph flow: each paragraph should answer the prior question or make the next question necessary. Add hinge sentences when sections read like a list.
13. Check the conclusion: return to the concrete carrier, name what was made visible or solved, state what transfers, and avoid a generic final sentence.
14. Replace rhythm with relation when the line still sounds good but has not named the mechanism.
15. Replace displaced copulas (“serves as,” “stands as,” “features,” “marks,” “represents”) with plain “is” or a specific action verb, unless the verb is doing concrete enumerating, defining, or locating work.
16. Cut hedged symmetry (“Whether X or Y,” “While X, Y is also important”) and commit to a specific reader and a named tradeoff.
17. Reject outline conclusion templates (“Despite challenges, X continues to thrive,” “Looking ahead, X will play a pivotal role”); replace with a carrier-bound ending or cut.
18. Thin decorative em-dash clusters; keep at most one earned dash insertion/pair per sentence unless multiple insertions carry distinct definitions.
19. End with a concrete remembered line.
```

## Avoid by default

Phrases:

```txt
In today's rapidly evolving landscape
In the realm of
When it comes to
At its core
Let's dive into
It's worth noting that
It's important to note that
A testament to
Not just X, but Y
Not only X, but also Y
Same X. Same Y. Different Z.
Not X. Y.
This is where X comes in
Whether you're X or Y
While X, Y is also important
Despite ongoing challenges, X continues to thrive
Looking ahead, X will play an increasingly pivotal role
In conclusion
Overall
Ultimately
I hope this helps
```

Words to review:

```txt
delve
realm
landscape as metaphor
tapestry
testament
pivotal
crucial
underscore
intricate
meticulous
multifaceted
nuanced as filler
foster
bolster
garner
showcase
highlight
emphasize
encompass
utilize
facilitate
transformative
groundbreaking
seamless
robust outside engineering context
```

Note: the word and phrase lists above are time-dated detectors. They reflect patterns observed in current model generations and will drift. Re-profile against a current human-vs-LLM corpus before adding or removing entries; `delve` is the cautionary example of a high-risk word whose frequency in LLM output dropped sharply during 2025.

Copula constructions such as `serves as` and `stands as` are intentionally not in the words list. They are two-word templates whose verdict depends on context: keep when the verb enumerates, defines, or locates; replace when it only inflates a copula.

## False-positive restraint

Treat detector hits as hypotheses, not verdicts. If the same sentence or nearby context supplies the mechanism, failure mode, measurement, or boundary that earns the term, return `Verdict: keep` and name that support.

Example:

```txt
Keep: "The queue is robust because each job has an idempotency key, a retry receipt, and a dead-letter cutoff."
Why: "robust" is an engineering qualifier earned by the deduplication, retry-tracking, and dead-letter mechanisms.
```

Do not offer a synonym-only rewrite just because a watch-list word appears.

## Staccato contrast test

Do not treat every short contrast as slop. First classify it. The test is whether each *side* of the contrast is supported by the prior prose, not just whether the topic of the contrast was mentioned.

```txt
Earned antithesis: both sides of the contrast are evidenced in the prior sentences. The contrast lands a distinction the reader can already verify. Keep or use once.
Compressed antithesis: one side is evidenced; the other side is a leap or a new claim. The cadence implies the contrast before evidence is given. Expand into the relation by naming the unsupported side directly.
Decorative antithesis: neither side is evidenced. The contrast supplies closure without content. Cut or replace.
```

Worked examples from the same source (joe.dev/posts/thinking-out-loud):

Earned: the post evidences both the failure of platforms (Medium pivoted, Twitter, LinkedIn) and the alternative (you owned your domain, you ran a server). The closer `That's what I want. My words, at a URL I own, without anyone in the middle.` lands a distinction whose both sides were shown.

Compressed: the prior sentence evidences movability — `ATproto lets you store structured records in a data server (your PDS) that you own and can move.` — but does not evidence intentionality. The closer `That's not incidental. It's the design.` asserts intentionality on cadence alone. The rewrite should name what makes portability the design (the protocol property that keeps record identity stable across PDS moves), not assert it.

Decorative: `The point is not the pelicans. The point is the process.` Neither the pelican carrier nor the process is evidenced before the contrast. Rewrite by naming the relation: `The pelican is useful because it gives the process a small, inspectable carrier.`

Rewrite rules:

```txt
replace rhythm with relation
prefer hypotaxis to parataxis
```

Typical moves:

```txt
Although X, Y.
Because X, Y.
When X changes, Y no longer means the same thing.
Where X only shows final state, Y records process.
X matters because it changes Y.
Once X is visible, Y becomes inspectable.
Because the concrete example is small/memorable/inspectable, it can carry the larger claim.
```

## Rewrite patterns

Bad:

```txt
This underscores the importance of durable execution.
```

Better:

```txt
The workflow can fail on step 4, retry only that step, and keep the previous outputs.
```

Bad:

```txt
Cloudflare is not just a CDN, but a platform.
```

Better:

```txt
Cloudflare turns the network boundary into a programmable runtime.
```

Bad:

```txt
This empowers teams to build seamless experiences.
```

Better:

```txt
The team can ship a WebSocket room without running a room server.
```

Weak ending:

```txt
A benchmark is stronger when you can inspect the run that produced it.
```

Better ending:

```txt
Because the pelican project is small enough to inspect and strange enough to remember, it works as a compact carrier for the larger claim: a benchmark is stronger when you can inspect the run that produced it.
```

## Final self-check

Before saying the edit is done:

```txt
Did I answer the user's actual request: review, rewrite, draft, or all three?
Did every flagged slop tell get a concrete rewrite or a reason to cut?
Does each rewrite name the mechanism, relation, actor/action/result, or concrete carrier?
Did I preserve earned compression instead of expanding it into bland explanation?
Did I avoid introducing new prestige abstractions or banned fallback phrases?
Did I run the same detectors on my rewrite that I ran on the source? In particular, does my rewrite reuse the cadence I just flagged (em-dash antithesis, "X isn't A, it's B", short parallel clauses) under different punctuation?
When a rewrite needs a fact not in the source (a name, a number, a tool, a mechanism), did I ask the author or recommend cutting, rather than inventing a confident-sounding specific?
```

For high-stakes prose, do one bounded judge-refine pass: score specificity, evidence fit, relation clarity, and rhythm on 1-5; improve the weakest dimension once.

## Critique output format

When reviewing existing prose, use:

```txt
Verdict: keep / revise / ask-author / reject
Slop tells:
Specificity missing:
Inflated claim:
Flow break:
Concrete rewrite:
Rewrite check:
Remembered line:
```

Use `ask-author` when the line is genuinely improvable but the fix would
need a fact the source paragraph does not supply: a tool name, a person,
a count, a timing claim, a named mechanism, a specific event. Name what
to ask in the `Concrete rewrite` slot — for example,
`Ask author: which coding tool? Cursor, Claude Code, Copilot, Aider, other?` —
and offer a fallback (cut, or keep with the next sentences carrying the
work). Do not invent the missing fact to fill the `Concrete rewrite`
slot. The same rule applies to any fallback rewrite inside an
`ask-author` block: a fallback that invents is worse than no fallback.

`Rewrite check` is mandatory. State whether your `Concrete rewrite`
(including any `ask-author` fallback) contains any of: rule-of-three,
X-not-Y / negative parallelism, em-dash antithesis, banned avoid-by-default
phrases, prestige adjectives, decorative closure ("That was the point",
"In conclusion", "Overall", "Ultimately"), or invented facts. If the
rewrite would have been a `revise` flag on a source paragraph, it should
be a `revise` flag on itself: rewrite again or escalate to `ask-author`.
If the rewrite passes, write `passes self-detectors`.

When classifying antithesis or staccato contrast, read the prior sentence
of the same paragraph first. If it supplies the mechanism the contrast
points at, the contrast is earned. Do not grade earned contrast as
compressed or decorative on the strength of cadence alone.

## Tone target

Prefer:

```txt
specific
source-grounded
direct
varied rhythm
concrete mechanisms
named tradeoffs
memorable line
```

Avoid:

```txt
safe essay voice
prestige abstraction
balanced filler
fake profundity
marketing fog
```
