"""One vocabulary for "did we observe it", one list of telemetry sources."""
import typing
import unittest

import agent_capabilities as caps
import observation_contracts as oc


class AvailabilityTests(unittest.TestCase):
    def test_every_legacy_spelling_reads_as_one_canonical_state(self):
        cases = {
            "complete": oc.Availability.COMPLETE,
            "partial": oc.Availability.PARTIAL,
            "incomplete": oc.Availability.PARTIAL,
            "unavailable": oc.Availability.UNAVAILABLE,
            "unknown": oc.Availability.UNAVAILABLE,
            "unobserved": oc.Availability.UNAVAILABLE,
            "missing": oc.Availability.UNAVAILABLE,
            "not_applicable": oc.Availability.NOT_APPLICABLE,
            "not-applicable": oc.Availability.NOT_APPLICABLE,
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertIs(oc.Availability.parse(raw), expected)

    def test_an_unknown_spelling_is_an_error(self):
        for raw in ("done", "", None, 1):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                oc.Availability.parse(raw)

    def test_only_complete_counts_as_observed(self):
        self.assertEqual([item for item in oc.Availability if item.observed],
                         [oc.Availability.COMPLETE])


class TelemetrySourceTests(unittest.TestCase):
    def test_cost_never_accepts_a_bare_estimate(self):
        # A cost estimate names its price table; the trigger path once
        # accepted "estimated" while the answer path rejected it.
        self.assertNotIn("estimated", oc.COST_SOURCES)
        self.assertIn("price_table_estimated", oc.COST_SOURCES)
        self.assertNotIn("price_table_estimated", oc.USAGE_SOURCES)

    def test_declared_capability_literals_stay_within_the_source_list(self):
        # The capability registry spells its sources as Literal types for ty;
        # they must name sources the harness can actually record.
        known = {source.value for source in oc.TelemetrySource}
        self.assertLessEqual(set(typing.get_args(caps.CostSupport)), known)
        self.assertLessEqual(set(typing.get_args(caps.TelemetryProvenance)),
                             set(oc.MEASUREMENT_PROVENANCE))


if __name__ == "__main__":
    unittest.main()
