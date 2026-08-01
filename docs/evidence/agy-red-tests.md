# Red-test evidence: agy protocol defects D1, D2, D3

Captured against the step-2 stub in `agy_contracts.py`, which deliberately
reproduces the semantics the earlier `agy-adapter` branch shipped. Step 4
replaces those internals; these failures are what it has to turn green.

```console
$ python -m unittest tests.test_agy_contracts -v
test_all_zero_counters_are_treated_as_absent (tests.test_agy_contracts.AbsentTelemetryIsNotZero.test_all_zero_counters_are_treated_as_absent) ... FAIL
test_auth_failure_usage_is_missing_not_zero (tests.test_agy_contracts.AbsentTelemetryIsNotZero.test_auth_failure_usage_is_missing_not_zero) ... FAIL
test_missing_usage_block_is_absent (tests.test_agy_contracts.AbsentTelemetryIsNotZero.test_missing_usage_block_is_absent) ... ok
test_real_usage_is_still_present (tests.test_agy_contracts.AbsentTelemetryIsNotZero.test_real_usage_is_still_present) ... ok
test_no_reported_model_does_not_borrow_the_requested_one (tests.test_agy_contracts.ModelIdentityUncertainty.test_no_reported_model_does_not_borrow_the_requested_one) ... ok
test_single_reported_model_resolves (tests.test_agy_contracts.ModelIdentityUncertainty.test_single_reported_model_resolves) ... ok
test_two_reported_models_do_not_collapse_to_the_first (tests.test_agy_contracts.ModelIdentityUncertainty.test_two_reported_models_do_not_collapse_to_the_first) ... ok
test_auth_failure_error_string_is_preserved (tests.test_agy_contracts.ProviderErrorSurvivesNonzeroExit.test_auth_failure_error_string_is_preserved) ... FAIL
test_auth_failure_is_not_a_complete_observation (tests.test_agy_contracts.ProviderErrorSurvivesNonzeroExit.test_auth_failure_is_not_a_complete_observation) ... ok
test_zero_exit_still_reports_provider_error (tests.test_agy_contracts.ProviderErrorSurvivesNonzeroExit.test_zero_exit_still_reports_provider_error) ... ok
test_completed_read_of_the_mounted_skill_is_activation (tests.test_agy_contracts.SearchIsNotActivation.test_completed_read_of_the_mounted_skill_is_activation) ... ok
test_read_and_search_partitions_are_disjoint (tests.test_agy_contracts.SearchIsNotActivation.test_read_and_search_partitions_are_disjoint) ... ok
test_search_only_run_is_not_a_clean_negative_either (tests.test_agy_contracts.SearchIsNotActivation.test_search_only_run_is_not_a_clean_negative_either) ... FAIL
test_search_only_run_is_not_recorded_as_activation (tests.test_agy_contracts.SearchIsNotActivation.test_search_only_run_is_not_recorded_as_activation) ... FAIL
test_search_only_run_produces_search_evidence (tests.test_agy_contracts.SearchIsNotActivation.test_search_only_run_produces_search_evidence) ... FAIL
test_search_tools_are_not_classified_as_file_reads (tests.test_agy_contracts.SearchIsNotActivation.test_search_tools_are_not_classified_as_file_reads) ... FAIL

======================================================================
FAIL: test_all_zero_counters_are_treated_as_absent (tests.test_agy_contracts.AbsentTelemetryIsNotZero.test_all_zero_counters_are_treated_as_absent)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/home/user/skill-eval-harness/tests/test_agy_contracts.py", line 107, in test_all_zero_counters_are_treated_as_absent
    self.assertEqual(
AssertionError: {'input_tokens': 0, 'output_tokens': 0, 't[57 chars]': 0} != {}
+ {}
- {'cache_read_tokens': 0,
-  'input_tokens': 0,
-  'output_tokens': 0,
-  'thinking_tokens': 0,
-  'total_tokens': 0} : zero-filled counters were carried through as if measured

======================================================================
FAIL: test_auth_failure_usage_is_missing_not_zero (tests.test_agy_contracts.AbsentTelemetryIsNotZero.test_auth_failure_usage_is_missing_not_zero)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/home/user/skill-eval-harness/tests/test_agy_contracts.py", line 96, in test_auth_failure_usage_is_missing_not_zero
    self.assertNotIsInstance(
AssertionError: AgyUsagePresent(counters={'input_tokens': 0, 'output_tokens': 0, 'thinking_tokens': 0, 'cache_read_tokens': 0, 'total_tokens': 0}) is an instance of <class 'agy_contracts.AgyUsagePresent'> : an authentication failure that never reached a model published provider-reported token counters; absent telemetry must not become a zero-valued measurement

======================================================================
FAIL: test_auth_failure_error_string_is_preserved (tests.test_agy_contracts.ProviderErrorSurvivesNonzeroExit.test_auth_failure_error_string_is_preserved)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/home/user/skill-eval-harness/tests/test_agy_contracts.py", line 129, in test_auth_failure_error_string_is_preserved
    self.assertEqual(
AssertionError: None != 'authentication failed or timed out' : agy exits 1 on an authentication failure while still emitting a structured error; gating the provider error on a zero exit code discards the only diagnosis the run produced

======================================================================
FAIL: test_search_only_run_is_not_a_clean_negative_either (tests.test_agy_contracts.SearchIsNotActivation.test_search_only_run_is_not_a_clean_negative_either)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/home/user/skill-eval-harness/tests/test_agy_contracts.py", line 79, in test_search_only_run_is_not_a_clean_negative_either
    self.assertIsInstance(observation, AgySkillObservationUnavailable)
AssertionError: AgySkillActivated(path='/WORKSPACE/.agents/skills/demo/SKILL.md') is not an instance of <class 'agy_contracts.AgySkillObservationUnavailable'>

======================================================================
FAIL: test_search_only_run_is_not_recorded_as_activation (tests.test_agy_contracts.SearchIsNotActivation.test_search_only_run_is_not_recorded_as_activation)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/home/user/skill-eval-harness/tests/test_agy_contracts.py", line 67, in test_search_only_run_is_not_recorded_as_activation
    self.assertNotIsInstance(
AssertionError: AgySkillActivated(path='/WORKSPACE/.agents/skills/demo/SKILL.md') is an instance of <class 'agy_contracts.AgySkillActivated'> : a run that only searched for the skill was recorded as having activated it, which inflates the trigger matrix

======================================================================
FAIL: test_search_only_run_produces_search_evidence (tests.test_agy_contracts.SearchIsNotActivation.test_search_only_run_produces_search_evidence)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/home/user/skill-eval-harness/tests/test_agy_contracts.py", line 60, in test_search_only_run_produces_search_evidence
    self.assertEqual(
AssertionError: Lists differ: [] != ['grep_search', 'skill_search']

Second list contains 2 additional elements.
First extra element 0:
'grep_search'

- []
+ ['grep_search', 'skill_search'] : completed search operations must still be observed, as searches

======================================================================
FAIL: test_search_tools_are_not_classified_as_file_reads (tests.test_agy_contracts.SearchIsNotActivation.test_search_tools_are_not_classified_as_file_reads)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/home/user/skill-eval-harness/tests/test_agy_contracts.py", line 51, in test_search_tools_are_not_classified_as_file_reads
    self.assertEqual(
AssertionError: Lists differ: [AgyFileRead(path='/WORKSPACE/.agents/skil[36 chars]='')] != []

First list contains 2 additional elements.
First extra element 0:
AgyFileRead(path='/WORKSPACE/.agents/skills/demo/SKILL.md')

+ []
- [AgyFileRead(path='/WORKSPACE/.agents/skills/demo/SKILL.md'),
-  AgyFileRead(path='')] : a run whose only skill-directory contact was grep_search and skill_search recorded a file read; search intent is being counted as a completed read

----------------------------------------------------------------------
Ran 16 tests in 0.004s

FAILED (failures=7)
```
