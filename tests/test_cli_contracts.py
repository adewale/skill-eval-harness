import argparse
import sys
import unittest
from pathlib import Path
from unittest import mock

import skill_benchmark as sb
from cli_contracts import (
    CLICommand,
    CLIInvocation,
    ValidatedLegacyCLIInvocation,
)
from manifest_contracts import ExecutionVariant, ModelId, Split


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


if __name__ == "__main__":
    unittest.main()
