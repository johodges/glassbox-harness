---
name: code-review
description: Review Python code, fix a failing test, or add a function to a project that has tests.
tools: [run_tests]
---
# Reviewing and changing code

1. Facts first: call run_tests on the project and check_code on the file. Their results are facts; your reading of the code is not.
2. Read the code with read_file before you change it or judge it. A failing test's defined_at says which file and line to read.
3. To fix code: rewrite the broken function with write_code, in the code, never in an existing test. Then call run_tests again.
4. To add a function: add it with write_code, add a test for it with write_code in the test file, then call run_tests.
5. To review: call review_code once with every finding as path:line: what is wrong. Cite only lines you have read. Do not report style.
6. In your answer, give each finding with its path:line and say what confirmed it: a test, a ruff rule, or only your reading.
