# Review rubric benchmark

Decision model: `subq-snap-1.1`  
**Main concern correct** (single-choice question): 7/9  
**Detection** (yes/no question for the right category): 7/7  
**Clean changes left unflagged**: 2/2  
**Extra categories flagged by yes/no on bad cases** (avg): 3.29 (noisy on its own, which is why findings are investigated and proven before they are reported)

| case | expected | result | risk | main concern | flagged (yes/no) |
|---|---|---|---|---|---|
| sec-signature | security | ✅ | Severe | security | correctness, security, error_handling, api, tests |
| err-swallow | error_handling | ✅ | High | error_handling | correctness, error_handling, api, stability, tests |
| perf-quadratic | performance | ✅ | Moderate | performance | performance |
| api-rename | api | ✅ | Moderate | api | api, stability, tests |
| tests-removed | tests | ✅ | Severe | error_handling | correctness, security, error_handling, api, tests |
| config-ci | config | ✅ | Severe | security | security, stability, performance, tests, config |
| stab-retry | stability | ✅ | High | stability | correctness, error_handling, api, stability, performance, tests |
| clean-docstring | none (clean) | ✅ | Low | none | - |
| clean-rename-local | none (clean) | ✅ | Low | none | - |
