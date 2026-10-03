"""Docs that publish a list the code can enumerate stay in step with the code.

The 2026-09 docs audit found the same fact stated in several places and
drifting: three different telemetry provenance lists, a "saturated" definition
the flag never meant, a critical failure "excluded from every mean" when the
code zeroes its rates. The docs now keep one owner per fact, and these tests
check each owned list against the code, keep a short list of phrases the code
contradicts from coming back, and keep the docs index complete.

Each test reads the owning doc section only, so a value mentioned in passing
elsewhere cannot satisfy it.
"""
import re
import unittest
from pathlib import Path

import completion_contracts as cc
import findings as fd
import gate_policy as gp
import observation_contracts as oc
import skill_benchmark as sb

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"


def read(relative):
    return (ROOT / relative).read_text(encoding="utf-8")


def section(text, heading):
    """The body under a markdown heading, up to the next heading of the same or higher level."""
    match = re.search(rf"(?m)^(#+) {re.escape(heading)}\s*$", text)
    if match is None:
        raise AssertionError(f"heading not found: {heading}")
    level = len(match.group(1))
    rest = text[match.end():]
    end = re.search(rf"(?m)^#{{1,{level}}} ", rest)
    return rest[:end.start()] if end else rest


def entry(text, term):
    """One glossary paragraph: the line that starts with **term**."""
    match = re.search(rf"(?m)^\*\*{re.escape(term)}\*\* — .*$", text)
    if match is None:
        raise AssertionError(f"glossary entry not found: {term}")
    return match.group(0)


def code_spans(text):
    return set(re.findall(r"`([^`]+)`", text))


def bullet(text, prefix):
    match = re.search(rf"(?m)^- {re.escape(prefix)}.*$", text)
    if match is None:
        raise AssertionError(f"bullet not found: {prefix}")
    return match.group(0)


def table_rows(text):
    """Cells of each body row of the first markdown table in ``text``."""
    rows = [line for line in text.splitlines() if line.startswith("|")]
    return [[cell.strip() for cell in row.strip("|").split("|")] for row in rows[2:]]


class EnumeratedListTests(unittest.TestCase):
    """A list the code enumerates is published once and matches the code."""

    def test_the_cost_telemetry_section_owns_every_source_list(self):
        body = section(read("docs/commands.md"), "Cost telemetry (tokens and dollars)")
        for label, values in (("`usage_normalized`", oc.USAGE_SOURCES),
                              ("`cost_normalized`", oc.COST_SOURCES),
                              ("The v3 `telemetry` envelope", oc.MEASUREMENT_PROVENANCE)):
            listed = code_spans(bullet(body, label))
            with self.subTest(list=label):
                self.assertLessEqual(set(values), listed)

    def test_the_glossary_defines_every_stop_class_and_served_model_value(self):
        vocabulary = read("docs/vocabulary.md")
        for term, enum in (("Stop class", cc.StopClass),
                           ("Served model check", cc.ServedModelCheck)):
            with self.subTest(term=term):
                self.assertLessEqual({item.value for item in enum},
                                     code_spans(entry(vocabulary, term)))

    def test_the_finding_kinds_table_is_the_registry(self):
        body = section(read("docs/commands.md"), "Finding kinds")
        rows = {cells[0].strip("`"): cells for cells in table_rows(body)}
        self.assertEqual(set(rows), {kind.value for kind in fd.FindingKind})
        for kind in fd.FindingKind:
            with self.subTest(kind=kind.value):
                self.assertEqual(rows[kind.value][1:3],
                                 [kind.subject.value, kind.severity.value])

    def test_the_glossary_lists_every_eval_health_mark_in_order(self):
        body = section(read("docs/vocabulary.md"), "Eval health")
        listed = re.findall(r"(?m)^(\d+)\. `([a-z-]+)`", body)
        self.assertEqual(listed, [(str(mark.number), mark.value) for mark in fd.EvalMark])

    def test_the_five_marks_table_maps_each_mark_to_its_registered_kinds(self):
        body = section(read("docs/comparing-with-claude-api-evals.md"), "Five marks of a lift eval")
        mapped = {cells[1].strip("`"): code_spans(cells[-1]) for cells in table_rows(body)}
        expected = {mark.value: {kind.value for kind in fd.FindingKind if kind.mark is mark}
                    for mark in fd.EvalMark}
        self.assertEqual(mapped, expected)

    def test_the_readiness_entry_names_exactly_the_blocking_kinds(self):
        readiness = entry(read("docs/vocabulary.md"), "Readiness")
        registered = {kind.value for kind in fd.FindingKind}
        named = code_spans(readiness) & registered
        self.assertEqual(named, {kind.value for kind in gp.READINESS.kinds})

    def test_the_grading_options_section_names_every_command_that_takes_them(self):
        parser = sb.build_arg_parser()
        subs = next(a for a in parser._actions if a.__class__.__name__ == "_SubParsersAction")
        grading = {"--judge-results", "--allow-scripts", "--strict", "--embed-cmd"}
        taking = {name for name, sub in subs.choices.items()
                  if grading <= {opt for action in sub._actions for opt in action.option_strings}}
        body = section(read("docs/commands.md"), "Grading options")
        first_sentence = body.strip().split(" take ", 1)[0]
        self.assertEqual(code_spans(first_sentence), taking)
        rows = {cells[0].split()[0].strip("`") for cells in table_rows(body)}
        self.assertEqual(rows, grading)

    def test_the_repository_layout_lists_every_packaged_module(self):
        pyproject = read("pyproject.toml")
        match = re.search(r"py-modules = \[(.*?)\]", pyproject, re.DOTALL)
        self.assertIsNotNone(match)
        modules = match.group(1)
        layout = section(read("README.md"), "Repository layout")
        missing = [name for name in re.findall(r'"([^"]+)"', modules)
                   if f"{name}.py" not in layout]
        self.assertEqual(missing, [])


def living_docs():
    """Maintained docs, minus the dated records (specs and audits in the docs
    index, the changelog, and the lessons log), which keep their history and
    carry correction notes instead."""
    index = read("docs/README.md")
    dated = set()
    for heading in ("Specs", "Audits"):
        dated |= set(re.findall(r"\]\(([\w.-]+\.md)\)", section(index, heading)))
    docs = [path for path in sorted(DOCS.glob("*.md")) if path.name not in dated]
    return [ROOT / "README.md", ROOT / "TODO.md", ROOT / "CONTRIBUTING.md",
            ROOT / "examples" / "demo-skill" / "README.md", *docs]


class ContradictedPhraseTests(unittest.TestCase):
    """Phrases the code now contradicts do not come back into a living doc."""

    # pattern -> living docs allowed to keep it, and why
    RETIRED = {
        # A critical failure zeroes the run's rates, which still count in every
        # mean; only the graded score is withheld.
        r"excluded from every mean": set(),
        # The saturated flag means both arms at 1.0, not a with-skill ceiling.
        r"\*\*Saturated\*\*[^.\n]{0,20}every `with_skill` run passes": set(),
        # Repeated judge runs report an agreement block.
        r"reports no disagreement rate": set(),
        r"whose majority merge hides the disagreement": set(),
        # Completion evidence spellings; upgrading.md names them as pre-release.
        r"backend-default": {"upgrading.md"},
        r"not-requested": {"upgrading.md"},
        r"`other`,? (or )?`unobserved`": set(),
        r'"stop_class": "unobserved"': set(),
    }

    def test_no_living_doc_repeats_a_contradicted_phrase(self):
        found = []
        for path in living_docs():
            text = path.read_text(encoding="utf-8")
            for pattern, allowed in self.RETIRED.items():
                if path.name in allowed:
                    continue
                # Prose wraps, so a space in a pattern matches any run of whitespace.
                for match in re.finditer(pattern.replace(" ", r"\s+"), text):
                    line = text.count("\n", 0, match.start()) + 1
                    found.append(f"{path.relative_to(ROOT)}:{line}: {match.group(0)!r}")
        self.assertEqual(found, [])


class DocsIndexTests(unittest.TestCase):
    def test_every_doc_is_linked_from_the_docs_index(self):
        index = read("docs/README.md")
        linked = set(re.findall(r"\]\(([\w.-]+\.md)(?:#[^)]*)?\)", index))
        missing = [path.name for path in sorted(DOCS.glob("*.md"))
                   if path.name != "README.md" and path.name not in linked]
        self.assertEqual(missing, [])


class RoadmapStatusTests(unittest.TestCase):
    """TODO.md owns status; the spec's Bucket 5 checkboxes must agree with it."""

    @staticmethod
    def states(text):
        return {number: mark == "x"
                for mark, number in re.findall(r"(?m)^- \[( |x)\] (?:\*\*)?(5\.\d+)\b", text)}

    def test_bucket_five_checkboxes_match_between_todo_and_the_spec(self):
        todo = self.states(section(read("TODO.md"), "Bucket 5 — eval health (can the eval show the lift, and whose failure is it?)"))
        spec = self.states(section(read("docs/eval-framework-roadmap-spec.md"),
                                   "Bucket 5 — eval health (from the /claude-api build-eval and hillclimb comparison)"))
        self.assertTrue(todo)
        self.assertEqual(todo, spec)


if __name__ == "__main__":
    unittest.main()
