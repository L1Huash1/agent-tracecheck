# Agent trace report

4 calls, 3 errors, 1 pending, 7 findings

Observed timestamp span: 450 ms.

| Level | Code | Finding |
| --- | --- | --- |
| warning | tool\_error | Tool call returned an error result. |
| warning | tool\_error | Tool call returned an error result. |
| warning | tool\_error | Tool call returned an error result. |
| warning | missing\_result | Tool call has no matching result in this trace. |
| warning | repeated\_call | Identical tool and arguments observed 3 times; check whether repetition is intended. |
| warning | error\_retry | Identical tool and arguments were called after an observed error; check the retry policy. |
| error | error\_budget | Observed 3 error results exceeds the error budget of 0. |

Recorded traces only; timing may be partial and repetitions may be intentional.
