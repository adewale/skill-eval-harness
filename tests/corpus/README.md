# Real-skill conformance corpus

Vendored copies of `SKILL.md` from the author's public skill repositories, used
as a conformance/fuzz corpus so the ablation parser and materializer are tested
against **real-world** YAML/Markdown (block-scalar `description: >`, varied
heading/fence/list shapes) rather than only synthetic fixtures.

Source (all `adewale/<repo>`, `main` branch):

| file | repo:path |
|---|---|
| `good-pr.SKILL.md` | good-pr:skills/good-pr/SKILL.md |
| `anti-slop-writing.SKILL.md` | anti-slop-writing:skills/anti-slop-writing/SKILL.md |
| `good-readme.SKILL.md` | good-readme:skills/good-readme/SKILL.md |
| `good-repo.SKILL.md` | good-repo:skills/good-repo/SKILL.md |
| `guardrails-skill.SKILL.md` | guardrails-skill:skills/guardrails/SKILL.md |
| `swiss-poster-skill.SKILL.md` | swiss-poster-skill:swiss-poster/SKILL.md |
| `testing-best-practices.SKILL.md` | testing-best-practices:testing-best-practices/SKILL.md |

Refresh:

```sh
curl -fsS https://raw.githubusercontent.com/adewale/good-pr/main/skills/good-pr/SKILL.md -o tests/corpus/good-pr.SKILL.md
# ...repeat per row above
```

`tests/test_skill_benchmark.py::SkillCorpusConformanceTests` consumes these.
