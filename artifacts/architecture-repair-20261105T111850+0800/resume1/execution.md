# Resume execution

T1 only: repair zero-exit loader false-pass; review application lease terminal/cancellation boundaries; update evidence/docs. No containers, guest package changes, GUI, deploy, commit or push. Existing dirty work and index preserved in before.json and before/.

Serial worker: MemoryMax=512MiB, MemoryHigh=384MiB, TasksMax=96, CPUQuota=100%, MemorySwapMax=0, LimitCORE=0. Independent guardian: MemoryMax=64MiB, MemoryHigh=48MiB, TasksMax=16, CPUQuota=100%, MemorySwapMax=0, 250ms sampling and fresh 30s admission. Worker deadline180s, guardian260s. Reserve1GiB plus full worst-case incremental budget and128MiB margin. Private HOME/XDG/TMP; desktop env removed, integration disabled. Logs20MiB each/200MiB run; write budget1GiB.

Previous attempt had an assertion failure without resource abort: glibc --list exit0 emitted missing symbol version. Preserve original failure. This retry adds diagnostic-aware classification and isolates the actual loader environment; unknown diagnostics remain unverified.

Stop only exact registered units after InvocationID/cgroup validation; BindsTo + heartbeat + RuntimeMaxSec cover guardian loss/deadline. No other loads launched concurrently. Stop after focused acceptance passes.
