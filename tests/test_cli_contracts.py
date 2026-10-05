import argparse
import ast
import builtins
import contextlib
import functools
import importlib.util
import inspect
import io
import sys
import tempfile
import textwrap
import types
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest import mock

from helpers import make_eval_repo, run_cli

import run_pi_trigger_eval as pi_runner
import run_trigger_matrix as tm
import skill_benchmark as sb
from agent_capabilities import (
    answer_entrypoint_implementations,
    surface_cli_options,
    surface_option_values,
)
from cli_contracts import (
    CLICommand,
    CLIInvocation,
    ValidatedLegacyCLIInvocation,
)
from manifest_contracts import ExecutionVariant, ModelId, Split

ROOT = Path(__file__).resolve().parents[1]


class CLIInvocationTests(unittest.TestCase):
    def parse(self, *arguments: str) -> argparse.Namespace:
        return sb.build_arg_parser().parse_args(list(arguments))

    def test_parser_and_command_enum_are_the_same_closed_surface(self):
        parser = sb.build_arg_parser()
        subparsers = next(
            action
            for action in parser._actions
            if action.__class__.__name__ == "_SubParsersAction"
        )
        self.assertEqual(set(subparsers.choices), {command.value for command in CLICommand})

    def test_grade_invocation_projects_paths_split_and_variants(self):
        namespace = self.parse(
            "grade",
            "evals/shared-benchmark.json",
            "--runs",
            "eval-runs/latest",
            "--split",
            "holdout",
            "--variant",
            "with_skill",
            "--variant",
            "ablation:docs",
        )

        invocation = CLIInvocation.from_namespace(namespace)

        self.assertIs(invocation.command, CLICommand.GRADE)
        self.assertIsInstance(invocation, ValidatedLegacyCLIInvocation)
        self.assertEqual(invocation.paths["manifest"], Path("evals/shared-benchmark.json"))
        self.assertEqual(invocation.paths["runs"], Path("eval-runs/latest"))
        self.assertEqual(invocation.split, Split("holdout"))
        self.assertEqual(
            invocation.variants,
            (ExecutionVariant("with_skill"), ExecutionVariant("ablation:docs")),
        )
        self.assertEqual(vars(invocation.to_legacy_namespace()), vars(namespace))

    def test_prepare_invocation_projects_unique_model_ids(self):
        invocation = CLIInvocation.from_namespace(
            self.parse(
                "prepare",
                "evals/shared-benchmark.json",
                "--models",
                "model-a, model-b",
                "--runs-per-variant",
                "2",
            )
        )

        self.assertEqual(invocation.models, (ModelId("model-a"), ModelId("model-b")))

    def test_repeated_path_arguments_are_frozen_paths(self):
        invocation = CLIInvocation.from_namespace(
            self.parse(
                "token-overhead",
                "first.json",
                "second.json",
                "--runs",
                "eval-runs/latest",
            )
        )

        self.assertEqual(
            invocation.paths["manifests"],
            (Path("first.json"), Path("second.json")),
        )

    def test_invalid_domain_values_fail_before_dispatch(self):
        invalid_arguments = (
            ("prepare", "manifest.json", "--runs-per-variant", "0"),
            ("prepare", "manifest.json", "--models", "model-a,model-a"),
            ("run-agent", "--agent", "codex", "--tasks", "tasks.jsonl", "--runs", "runs", "--timeout", "0"),
            ("contamination", "manifest.json", "--runs", "runs", "--overlap-threshold", "1.1"),
            ("render-viewer", "--benchmark", "benchmark.json", "--out", "viewer.html", "--port", "70000"),
        )
        for arguments in invalid_arguments:
            with self.subTest(arguments=arguments):
                namespace = self.parse(*arguments)
                with self.assertRaises(ValueError):
                    CLIInvocation.from_namespace(namespace)

    def test_unknown_command_is_not_an_invocation(self):
        # A hand-built namespace on purpose: the parser rejects an unknown
        # subcommand itself (exit 2), so no argv reaches from_namespace with
        # one; this pins the boundary's own guard for programmatic callers.
        with self.assertRaisesRegex(ValueError, "unknown CLI command"):
            CLIInvocation.from_namespace(argparse.Namespace(cmd="surprise"))

    def test_legacy_zero_values_keep_their_established_meaning(self):
        cases = (
            ("error-analysis", "--benchmark", "benchmark.json", "--limit", "0"),
            ("profile-skill", "manifest.json", "--max-references", "0"),
            ("cost-summary", "--manifest", "manifest.json", "--runs", "runs",
             "--top", "0"),
        )
        for arguments in cases:
            with self.subTest(arguments=arguments):
                invocation = CLIInvocation.from_namespace(self.parse(*arguments))
                self.assertIn(0, vars(invocation.to_legacy_namespace()).values())

    def test_legacy_argument_bag_is_deeply_frozen_and_thawed(self):
        namespace = self.parse(
            "grade", "manifest.json", "--runs", "runs",
            "--variant", "with_skill")
        invocation = CLIInvocation.from_namespace(namespace)
        namespace.variant.append("without_skill")
        self.assertEqual(invocation.arguments["variant"], ("with_skill",))

        projected = invocation.to_legacy_namespace()
        projected.variant.append("without_skill")
        self.assertEqual(invocation.arguments["variant"], ("with_skill",))

    def test_main_reports_boundary_failures_as_argparse_errors(self):
        argv = [
            "skill-benchmark",
            "prepare",
            "manifest.json",
            "--runs-per-variant",
            "0",
        ]
        with mock.patch.object(sys, "argv", argv), self.assertRaises(SystemExit) as raised:
            sb.main()
        self.assertEqual(raised.exception.code, 2)

    def test_min_lift_is_one_pass_rate_difference_for_every_command(self):
        # benchmark and audit-manifest feed --min-lift to the same noise check,
        # so one validator at this edge holds both to (0, 1] before any work.
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            manifest = make_eval_repo(Path(td))
            runs = Path(td) / "runs"
            commands = (("benchmark", manifest, "--runs", runs),
                        ("audit-manifest", manifest, "--out", Path(td) / "audit.json"))
            for command in commands:
                for value in ("10", "0", "-0.1"):
                    with self.subTest(command=command[0], value=value):
                        code, _, stderr = run_cli(*command, "--min-lift", value)
                        self.assertEqual(code, 2)
                        self.assertIn("min-lift must be a pass-rate difference in (0, 1]", stderr)
            self.assertFalse((Path(td) / "audit.json").exists())
        accepted = CLIInvocation.from_namespace(self.parse("audit-manifest", "m.json", "--min-lift", "1"))
        self.assertEqual(accepted.arguments["min_lift"], 1.0)


# --------------------------------------------------------------------------- #
# Argument wiring: what each command reads from the namespace its parser builds
# --------------------------------------------------------------------------- #

# Destinations a parser defines that no handler reads. Each one's --help text
# says it is a no-op, so the flag is accepted on purpose. Shrink-only.
DOCUMENTED_NO_OP_FLAGS = frozenset({
    ("export-jetty", "dry_run"),         # "export never performs network calls"
    ("import-trace", "write_metadata"),  # "deprecated compatibility flag"
    ("run-jetty", "concurrency"),        # "reserved; ... runs sequentially"
})

_UNKNOWN = object()
_NOT_IN_OR = object()


class NamespaceReads:
    """Every option a function reads from a namespace, following the namespace
    into each function it is passed to, plus every flow the walk could not
    follow (so the check fails closed instead of missing a read)."""

    def __init__(self) -> None:
        self.reads: dict[str, set[str]] = {}
        self.defaults: list[tuple[str, Any, Any, str]] = []
        self.unresolved: list[str] = []
        self.visited: set[tuple[str, str, frozenset[str]]] = set()

    def read(self, dest: str, site: str) -> None:
        self.reads.setdefault(dest, set()).add(site)


def _function_node(function: Callable[..., Any]) -> ast.FunctionDef:
    node = ast.parse(textwrap.dedent(inspect.getsource(function))).body[0]
    if not isinstance(node, ast.FunctionDef):
        raise TypeError(f"{function!r} is not a plain function")
    return node


def _resolve(function: Callable[..., Any], node: ast.expr) -> Any:
    """The object a call target names in the module that defines `function`."""
    scope = function.__globals__
    if isinstance(node, ast.Name):
        return scope.get(node.id, getattr(builtins, node.id, None))
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        owner = scope.get(node.value.id)
        if isinstance(owner, types.ModuleType):
            return getattr(owner, node.attr, None)
    return None


def _constant(function: Callable[..., Any], node: ast.expr) -> Any:
    """A literal, or a module-level name's value; _UNKNOWN for anything else."""
    try:
        return ast.literal_eval(node)
    except ValueError:
        pass
    if isinstance(node, ast.Name) and node.id in function.__globals__:
        return function.__globals__[node.id]
    return _UNKNOWN


def assignment(node: ast.AST) -> tuple[list[ast.expr], ast.expr | None]:
    """The targets and value of `x = v` or `x: T = v`; ([], None) for anything else."""
    if isinstance(node, ast.Assign):
        return node.targets, node.value
    if isinstance(node, ast.AnnAssign) and node.value is not None:
        return [node.target], node.value
    return [], None


def collect_namespace_reads(function: Callable[..., Any], names: set[str],
                            found: NamespaceReads | None = None) -> NamespaceReads:
    """Walk `function`, where `names` hold the parsed namespace: record
    `ns.option` and `getattr/hasattr(ns, "option")`, and recurse into every
    function that receives the namespace, under that function's own parameter
    name."""
    found = NamespaceReads() if found is None else found
    function = inspect.unwrap(function)
    key = (function.__module__, function.__qualname__, frozenset(names))
    if key in found.visited:
        return found
    found.visited.add(key)
    node = _function_node(function)
    offset = function.__code__.co_firstlineno - 1
    parents = {child: parent for parent in ast.walk(node) for child in ast.iter_child_nodes(parent)}
    aliases = set(names)
    for sub in ast.walk(node):
        targets, value = assignment(sub)
        if isinstance(value, ast.Name) and value.id in aliases:
            aliases.update(t.id for t in targets if isinstance(t, ast.Name))

    def held(expr: ast.expr) -> bool:
        return isinstance(expr, ast.Name) and expr.id in aliases

    def site(sub: ast.AST) -> str:
        return f"{function.__module__}.{function.__qualname__}:{offset + getattr(sub, 'lineno', 1)}"

    followed: set[int] = set()
    for sub in ast.walk(node):
        targets, value = assignment(sub)
        if isinstance(sub, ast.Attribute) and held(sub.value):
            found.read(sub.attr, site(sub))
            followed.add(id(sub.value))
        elif value is not None and held(value):
            followed.add(id(value))
            for target in targets:
                if not isinstance(target, ast.Name):
                    found.unresolved.append(f"{site(sub)}: namespace stored in "
                                            f"{ast.unparse(target)}, which the walk cannot follow")
        elif isinstance(sub, ast.Call):
            passed = [arg for arg in [*sub.args, *(kw.value for kw in sub.keywords)] if held(arg)]
            if not passed:
                continue
            followed.update(id(arg) for arg in passed)
            _follow_call(function, sub, held, site(sub), parents, found)
    for sub in ast.walk(node):
        if held(sub) and isinstance(sub.ctx, ast.Load) and id(sub) not in followed:
            found.unresolved.append(f"{site(sub)}: namespace escapes the walk")
    return found


def _follow_call(function: Callable[..., Any], call: ast.Call, held: Callable[[ast.expr], bool],
                 where: str, parents: dict[ast.AST, ast.AST], found: NamespaceReads) -> None:
    target = _resolve(function, call.func)
    if target in (getattr, hasattr) and call.args and held(call.args[0]):
        name = _constant(function, call.args[1]) if len(call.args) > 1 else _UNKNOWN
        if not isinstance(name, str):
            found.unresolved.append(f"{where}: {target.__name__} with a computed name")
            return
        found.read(name, where)
        if target is getattr and len(call.args) == 3:
            found.defaults.append((name, _constant(function, call.args[2]),
                                   _or_fallback(function, call, parents), where))
        return
    if target is surface_option_values and call.args and held(call.args[0]):
        # Backend options are added to a parser by add_surface_cli_options and
        # read back here by the same surface name.
        for option in surface_cli_options(_constant(function, call.args[1])):
            found.read(option.dest, where)
        return
    skip_self = isinstance(target, type)
    if skip_self:
        target = target.__init__
    if not isinstance(target, types.FunctionType):
        found.unresolved.append(f"{where}: namespace passed to {ast.unparse(call.func)}")
        return
    parameters = list(inspect.signature(target).parameters.values())[1 if skip_self else 0:]
    positional = [p.name for p in parameters
                  if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    by_keyword = {p.name for p in parameters if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)}
    receivers: set[str] = set()
    for index, arg in enumerate(call.args):
        if any(isinstance(earlier, ast.Starred) for earlier in call.args[:index + 1]) and held(arg):
            found.unresolved.append(f"{where}: namespace passed after *args")
        elif held(arg) and index < len(positional):
            receivers.add(positional[index])
        elif held(arg):
            found.unresolved.append(f"{where}: namespace lands in *args of {target.__qualname__}")
    for keyword in call.keywords:
        if held(keyword.value) and keyword.arg in by_keyword:
            receivers.add(keyword.arg)
        elif held(keyword.value):
            found.unresolved.append(f"{where}: namespace lands in **kwargs of {target.__qualname__}")
    if receivers:
        collect_namespace_reads(target, receivers, found)


def _or_fallback(function: Callable[..., Any], call: ast.Call,
                 parents: dict[ast.AST, ast.AST]) -> Any:
    """What `getattr(ns, "x", D) or F` falls back to: F's value, _UNKNOWN when
    F is not a constant, or _NOT_IN_OR when the read is not an `or` operand."""
    parent = parents.get(call)
    if (isinstance(parent, ast.BoolOp) and isinstance(parent.op, ast.Or)
            and parent.values[0] is call):
        return _constant(function, parent.values[1])
    return _NOT_IN_OR


def _same_default(handler_default: Any, fallback: Any, parser_default: Any) -> bool:
    """Whether a namespace without the option behaves like the parser default."""
    if handler_default is _UNKNOWN:
        return True
    if fallback is _NOT_IN_OR:
        falsy_flag = (None, False)
        return handler_default == parser_default or (
            handler_default in falsy_flag and parser_default in falsy_flag)
    if fallback is _UNKNOWN:
        return handler_default == parser_default or not (handler_default or parser_default)
    return (handler_default or fallback) == (parser_default or fallback)


def wiring_findings(parser: argparse.ArgumentParser, handler: Callable[..., Any],
                    namespace_names: set[str], *, command: str,
                    inherited: frozenset[str] = frozenset()) -> list[str]:
    """Everything that breaks the parser -> handler wiring of one command."""
    defined = {action.dest: action.default for action in parser._actions if action.dest != "help"}
    found = collect_namespace_reads(handler, namespace_names)
    findings = list(found.unresolved)
    for dest, sites in sorted(found.reads.items()):
        if dest not in defined and dest not in inherited:
            findings.append(f"reads {dest!r}, which the parser does not define ({min(sites)})")
    for dest in sorted(set(defined) - set(found.reads)):
        if (command, dest) not in DOCUMENTED_NO_OP_FLAGS:
            findings.append(f"defines {dest!r}, which no handler reads")
    for dest, default, fallback, where in found.defaults:
        if dest in defined and not _same_default(default, fallback, defined[dest]):
            findings.append(
                f"defaults {dest!r} to {default!r} when absent but the parser "
                f"defaults it to {defined[dest]!r} ({where})")
    return findings


def skill_benchmark_handlers() -> dict[str, Callable[..., Any]]:
    """main()'s dispatch: its built-in handler table (read from main's source)
    plus the answer entrypoints the backend registry resolves."""
    handlers: dict[str, Callable[..., Any]] = {}
    for node in ast.walk(_function_node(sb.main)):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if (isinstance(key, ast.Attribute) and isinstance(key.value, ast.Name)
                        and key.value.id == "CLICommand" and isinstance(value, ast.Name)):
                    handlers[CLICommand[key.attr].value] = getattr(sb, value.id)
    handlers.update(answer_entrypoint_implementations())
    return handlers


def subcommand_parsers(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    return next(action.choices for action in parser._actions
                if isinstance(action, argparse._SubParsersAction))


# The check's own known-good and known-bad inputs: one small parser, a handler
# wired to it correctly (directly and through an annotated alias), one carrying
# a misspelled read two calls deep under a renamed parameter, a getattr default
# that drifted from the parser, and a flag nobody reads, and handlers that put
# the namespace where the walk cannot follow it (a dict, a list slot, an object
# attribute).
def _planted_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest")
    parser.add_argument("--leakage-min-chars", type=int, default=4)
    parser.add_argument("--strict", action="store_true")
    return parser


def _wired_handler(args):
    return _wired_helper(args.manifest, options=args)


def _wired_helper(manifest, *, options):
    return manifest, getattr(options, "leakage_min_chars", 4), getattr(options, "strict", False)


def _miswired_handler(args):
    return _miswired_helper(args.manifest, options=args)


def _miswired_helper(manifest, *, options):
    return manifest, getattr(options, "not_a_flag", None), getattr(options, "leakage_min_chars", 3)


def _escaping_handler(args):
    return {"args": args}


def _annotated_alias_handler(args):
    ns: argparse.Namespace = args
    return _wired_helper(ns.manifest, options=ns)


def _subscript_store_handler(args):
    holder = [None]
    holder[0] = args
    return getattr(holder[0], "undefined_flag", None)


def _attribute_store_handler(args):
    box = types.SimpleNamespace()
    box.args = args
    return box.args.undefined_flag


class ArgumentWiringTests(unittest.TestCase):
    """A handler that reads an option with `getattr(args, "x", default)` runs at
    that default in production whenever its parser lacks --x, while a test that
    hand-builds the namespace sets x and passes. These tests read every option
    each command's handler (and every helper it hands the namespace to) reads,
    and hold it to the parser the user actually reaches."""

    def test_every_skill_benchmark_command_reads_only_what_its_parser_defines(self):
        subparsers = subcommand_parsers(sb.build_arg_parser())
        handlers = skill_benchmark_handlers()
        self.assertEqual(set(handlers), set(subparsers))
        findings = {}
        for command, subparser in sorted(subparsers.items()):
            handler = handlers[command]
            namespace = next(iter(inspect.signature(handler).parameters))
            problems = wiring_findings(subparser, handler, {namespace}, command=command,
                                       inherited=frozenset({"cmd"}))
            if problems:
                findings[command] = problems
        self.assertEqual(findings, {})

    def test_trigger_runners_read_only_what_their_parsers_define(self):
        for runner in (tm, pi_runner):
            with self.subTest(runner=runner.__name__):
                self.assertEqual(
                    wiring_findings(runner.build_arg_parser(), runner.main, {"args"},
                                    command=runner.__name__), [])

    def test_the_check_passes_a_wired_handler_and_names_each_planted_fault(self):
        for handler in (_wired_handler, _annotated_alias_handler):
            with self.subTest(handler=handler.__name__):
                self.assertEqual(
                    wiring_findings(_planted_parser(), handler, {"args"}, command="planted"), [])
        findings = wiring_findings(_planted_parser(), _miswired_handler, {"args"}, command="planted")
        self.assertEqual(len(findings), 3, findings)
        self.assertRegex(findings[0], r"^reads 'not_a_flag', which the parser does not define "
                                      r"\(.*_miswired_helper:\d+\)$")
        self.assertEqual(findings[1], "defines 'strict', which no handler reads")
        self.assertRegex(findings[2], r"^defaults 'leakage_min_chars' to 3 when absent but the "
                                      r"parser defaults it to 4 \(.*_miswired_helper:\d+\)$")

    def test_the_check_fails_closed_when_the_namespace_escapes_the_walk(self):
        cases = {
            _escaping_handler: r"_escaping_handler:\d+: namespace escapes the walk$",
            _subscript_store_handler: (r"_subscript_store_handler:\d+: namespace stored in "
                                       r"holder\[0\], which the walk cannot follow$"),
            _attribute_store_handler: (r"_attribute_store_handler:\d+: namespace stored in "
                                       r"box\.args, which the walk cannot follow$"),
        }
        for handler, expected in cases.items():
            with self.subTest(handler=handler.__name__):
                findings = wiring_findings(_planted_parser(), handler, {"args"}, command="planted")
                self.assertRegex(findings[0], expected)

    def test_no_parser_the_package_builds_accepts_an_abbreviated_option(self):
        # argparse matches a unique prefix of a long option by default, so a
        # renamed flag still answers to its old spelling's prefix, a user's
        # abbreviation silently works, and no argv test can catch a rename that
        # lengthens a flag. Every parser and subparser is collected as it is
        # built, by each console script's parser builder and by each script's
        # and example's own `--help` path, so a new one is covered unedited.
        built: list[argparse.ArgumentParser] = []
        original_init = argparse.ArgumentParser.__init__

        def recording_init(parser, *args, **kwargs):
            original_init(parser, *args, **kwargs)
            built.append(parser)

        sources = {"skill-benchmark": sb.build_arg_parser, "skill-trigger-matrix": tm.build_arg_parser,
                   "skill-pi-trigger-eval": pi_runner.build_arg_parser}
        for path in sorted([*ROOT.glob("scripts/*.py"), *ROOT.glob("examples/*/*.py")]):
            if "argparse" in path.read_text(encoding="utf-8"):
                sources[str(path.relative_to(ROOT))] = functools.partial(_run_script_help, path)
        abbreviating: dict[str, list[str]] = {}
        with mock.patch.object(argparse.ArgumentParser, "__init__", recording_init):
            for source, build in sources.items():
                with self.subTest(source=source):
                    built.clear()
                    build()
                    self.assertTrue(built, f"{source} built no parser")
                    abbreviating[source] = sorted(parser.prog for parser in built if parser.allow_abbrev)
        self.maxDiff = None
        self.assertEqual({source: progs for source, progs in abbreviating.items() if progs}, {})

    def test_an_abbreviated_option_is_an_unrecognized_argument(self):
        # Each console script, at the command line: `--judge-res` for
        # `--judge-results`, `--runs-per` for `--runs-per-query`.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest = make_eval_repo(root)
            judged = root / "judge-results.jsonl"
            judged.write_text("", encoding="utf-8")
            code, _, stderr = run_cli("grade", manifest, "--runs", root / "runs", "--judge-res", judged)
            self.assertEqual(code, 2, stderr)
            self.assertIn("unrecognized arguments: --judge-res", stderr)
            for runner in (tm, pi_runner):
                with self.subTest(runner=runner.__name__), \
                     mock.patch.object(sys, "argv", [runner.__name__, str(manifest), "--runs-per", "1",
                                                     "--out", str(root / "report.json")]), \
                     contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit) as ctx:
                    runner.main()
                self.assertEqual(ctx.exception.code, 2)
                self.assertIn("unrecognized arguments: --runs-per", err.getvalue())


def _run_script_help(path: Path) -> None:
    """Build a script's parser the way a user does, through `--help`."""
    spec = importlib.util.spec_from_file_location(f"_abbrev_probe_{path.stem}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with mock.patch.object(sys, "argv", [str(path), "--help"]), \
            contextlib.redirect_stdout(io.StringIO()), contextlib.suppress(SystemExit):
        module.main()


if __name__ == "__main__":
    unittest.main()
