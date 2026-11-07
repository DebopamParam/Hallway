# fix/daypass-output-cap proofs

Real model: vercel:deepseek/deepseek-v4.1-flash-1017, DAYPASS_PRICE_PER_MTOK=0.30,1.20, local fake stack only.
Flow (judge_cap.py --two-runs): a judge signs up on a Day pass, the worker runs once (first round), the judge asks
"Who should I meet here?", the worker runs once more. run_logged.py wraps the real worker and logs every model
response (max_tokens sent, finish_reason, output and reasoning tokens, tool-call arg sizes).

- before-1: main, single run (question queued before the first round). No cut-off that time.
- before-2: main, two runs. The reply hit finish_reason=length at out=1024 and the judge got no answer.
- sizing-3072-*: an intermediate fixed cap of 3072. after-2 shows propose_shortlist with an html page: 6010 chars, out=2300.
  That's why the final fix sizes each reply to the budget instead of a fixed cap.
- after-1, after-2: the final branch. max_tokens=8192, a 1105-token reply (1016 reasoning) completes, and the shortlist lands.
- pytest-*.txt: suite results; pytest-new-tests-on-main.txt is the two new tests failing against main's worker.
