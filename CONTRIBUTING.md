# Contributing

Use synthetic traces in issues and tests. Do not upload customer traces, credentials, hidden model reasoning, or private tool arguments.

```sh
python -m pip install -e .
python -m unittest discover -s tests -v
```

Keep analysis deterministic and offline. Explain whether a new finding is evidence, an inference, or a budget threshold. Include regressions for concurrency and missing timestamps when changing retry/timing logic. Native format adapters should state exactly which fields they map and what information is lost.
