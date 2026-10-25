# Case Studies: Manual Rust Migrations of Production Systems

**Topic:** Lessons and patterns from human-driven (non-AI) Rust migrations, transferable to an automated Python→Rust framework.
**Compiled:** 2026-10-25. All claims below were verified the same day against live web sources via Tavily search + full-text extraction of the primary URLs. Verified excerpts are quoted verbatim with their source URL. Items that could **not** be verified are explicitly flagged with **⚠️ UNVERIFIED**.

**Coverage matrix:** 12 URLs full-text extracted (Discord ×3, Cloudflare ×2, Figma ×3, AWS ×3, Dropbox ×2, npm ×1, 1Password ×1) plus corroborating searches for every case.

---

## 1. Discord — Go→Rust (Read States service) and Rust NIFs inside Elixir

### What
Discord's core real-time stack is Elixir/Erlang. Three relevant documented migrations:
- **"How Discord Scaled Elixir to 5,000,000 Concurrent Users"** (2017) — scaling the guild/presence infrastructure; mostly Elixir-level optimization, with the *seed* of the "drop into a faster language" pattern.
- **"Using Rust to Scale Elixir for 11 Million Concurrent Users"** (Matt Nowack, Feb 2020) — a Rust NIF (via Rustler) implementing the `SortedSet` data structure for guild member lists.
- **"Why Discord is switching from Go to Rust"** (Jesse Howarth, 2020) — full reimplementation of the **Read States** service from Go to Rust.

### Why (drivers)
- **Go→Rust: GC pause tail latency.** The Read States service (user read/unread state; LRU cache backed by Cassandra, "tens of thousands of database writes per second") showed latency and CPU spikes roughly **every 2 minutes**: *"In the picture below, you can see the response time and system cpu for a peak sample time frame for the Go service. As you might notice, there are latency and CPU spikes roughly every 2 minutes."* ([discord.com/blog/why-discord-is-switching-from-go-to-rust](https://discord.com/blog/why-discord-is-switching-from-go-to-rust))
  - Root cause chain: Go forces a GC run at least every 2 minutes; their heap was dominated by a large LRU cache, and *"the garbage collector needed to scan the entire LRU cache in order to determine if the memory was truly free from references."* Critically: *"These latency spikes definitely smelled like garbage collection performance impact, but we had written the Go code very efficiently and had very few allocations."* — i.e., the problem was **working-set size × GC scan cost**, not allocation churn.
- **Elixir→Rust NIFs: per-operation speed inside a working system.** In the 5M-user post, `send/2` cost *"30μs to 70us"* due to Erlang de-scheduling, so publishing to a huge guild *"could take anywhere from 900ms to 2.1s"* at peak; ETS copies were ~7μs per read but still *"17.5 seconds"* total for ring lookups ([discord.com/blog/how-discord-scaled-elixir-to-5-000-000-concurrent-users](https://discord.com/blog/how-discord-scaled-elixir-to-5-000-000-concurrent-users)). The 11M post replaced an Elixir data structure with a Rust NIF because *"the library operations all clocked in well under the 1 millisecond threshold"* while staying safe inside the BEAM ([discord.com/blog/using-rust-to-scale-elixir-for-11-million-concurrent-users](https://discord.com/blog/using-rust-to-scale-elixir-for-11-million-concurrent-users)).

### Process
- **Read States (Go→Rust) — iterative mitigation first, then reimplement, then incremental cut-over:**
  1. First **profiled and tuned the existing service**: dynamic GOGC endpoint, pre-allocation, partitioned LRU caches. Shrinking the cache *"resulted in smaller spikes"* but *"resulted in higher 99th latency times"* (more cache misses → more DB loads). They lived with the compromise: *"Not completely satisfied, but satisfied enough and with bigger fish to fry, we left the service running like this for quite some time."*
  2. Then reimplemented in Rust — Discord already had Rust experience (client-side Go Live video encoding, Elixir NIFs), so this was a **single-service rewrite by a team with prior Rust familiarity**.
  3. **Rollout was load-test + canary based** (the post does *not* describe shadow traffic mirroring): *"Satisfied, we decided to roll out the service. The launch was fairly seamless because we load tested. We put it out to a single canary node, found a few edge cases that were missing, and fixed them. Soon after that we rolled it out to the entire fleet."*
  4. Post-launch they **raised the LRU cache capacity back up**, now that GC scanning no longer degraded latency ("Raising the cache capacity" section: Go purple vs Rust blue production graphs).
- **NIF path (Elixir→Rust):** explore in-language options (BIFs, packages) first, then write a Rust NIF with Rustler's safety guarantees (*"we could just use the built-in Rustler guarantees and not need to worry about reductions or yielding. The SortedSet module looks to the caller to just be a vanilla Elixir module that performs crazy fast."*), open-sourced as `sorted_set`.

### Results (quantified)
- Go→Rust: *"Even with just basic optimization, Rust was able to outperform the hyper hand-tuned Go version."* After profiling: *"we were able to beat Go on every single performance metric. Latency, CPU, and memory were all better in the Rust version."* The 2-minute sawtooth spikes disappeared; cache capacity was restored. (The post shows production graphs rather than tabulated numbers.)
- 11M post: *"Since deploying SortedSet, we've seen performance improve across the board with no impact to memory pressure. We learned that Rust and Elixir can work side by side to operate in extremely tight performance constraints."*
- Scale context (Elixir Foundation, Oct 2020): *"more than 12 million concurrent users… more than 26 million WebSocket events to clients per second"*; *"Discord completely phased out Go after a short foray, Rust has proven to be an excellent addition to their toolbox, boosted by its ability to play well with Elixir and Python."* ([elixir-lang.org/blog/2020/10/08/real-time-communication-at-scale-with-elixir-at-discord](https://elixir-lang.org/blog/2020/10/08/real-time-communication-at-scale-with-elixir-at-discord))

### Pitfalls / notes
- GC tuning only trades spike size vs p99 miss rate — a *structural* fix (no-GC language) was required.
- Smaller scoped lesson: they reimplemented **one service**, not the fleet; Elixir remained the core.
- ⚠️ UNVERIFIED: "shadow testing" — the primary post describes load testing + canary, not request mirroring/shadowing.

---

## 2. Cloudflare — Pingora (nginx→Rust, ~1T requests/day)

### What
Pingora is Cloudflare's in-house asynchronous Rust HTTP proxy, built to replace nginx/OpenResty in their edge/anomaly of services; it *"serves over 1 trillion requests a day"*. The framework was **open-sourced Feb 2024** ([blog.cloudflare.com/how-we-built-pingora-the-proxy-that-connects-cloudflare-to-the-internet/](https://blog.cloudflare.com/how-we-built-pingora-the-proxy-that-connects-cloudflare-to-the-internet/); [blog.cloudflare.com/pingora-open-source/](https://blog.cloudflare.com/pingora-open-source/); open-sourcing corroborated by [Phoronix](https://www.phoronix.com/news/CloudFlare-Pingora-No-Nginx)).

### Why (drivers)
- **nginx architectural limits:** Cloudflare *"evaluated each of these options every quarter for the past few years"* (extend nginx vs third-party vs build own); at some point *"building our own proxy's return on investment seemed worth it."* Documented pain: nginx's worker/process model makes it hard to share/rebalance connection state (across config reloads), its configuration language is restrictive for custom logic, and OpenResty/Lua adds copying + GC overhead.
- **Zero-copy efficiency vs Lua:** *"in NGINX/OpenResty, when the Lua code wants to access an HTTP header, it has to read it from the NGINX C struct, allocate a Lua string and then copy it to the Lua string. Afterwards, Lua has to garbage-collect its new string as well. In Pingora, it would just be a direct string access."*
- **Memory safety:** *"We chose Rust as the language of the project because it can do what C can do in a memory safe way without compromising performance."* and *"Rust's memory-safe semantics guard us from undefined behavior… we can develop features at a higher cadence and not be burdened by memory safety and hard to diagnose crashes."*

### Process (patterns they built)
- New **multi-threaded architecture** (not nginx's multi-process-per-core model), async Rust on Tokio, their own Rust HTTP library.
- Framework abstractions later open-sourced as crates (verified from [github.com/cloudflare/pingora](https://github.com/cloudflare/pingora) README): `pingora` (public-facing), `pingora-core`, `pingora-proxy`, `pingora-http`, `pingora-error`, `pingora-timeout`, `pingora-ketama` (consistent hashing), `pingora-limits`, `pingora-load-balancing`, `pingora-memory-cache` ("Async in-memory caching with cache lock to prevent cache stampede"), `pingora-openssl/boringssl/s2n/rustls`, plus `tinyufo` cache. Feature highlights: *"Async Rust: fast and reliable; HTTP 1/2 end to end proxy; TLS over OpenSSL, BoringSSL, s2n-tls, or rustls(experimental); gRPC and websocket proxying; Graceful reload; Customizable load balancing and failover strategies."*
- **Zero-copy** and **structured concurrency/offloading** are the documented performance idioms (direct string access, off-the-shelf middleware hooks, graceful reload for incremental fleet rollouts).

### Results (quantified)
- *"serves over 1 trillion requests a day … requiring only a third of the CPU and memory resources of our previous proxy infrastructure."*
- Reliability: *"Since Pingora's inception we've served a few hundred trillion requests and have yet to crash due to our service code."*
- Open-source README: *"battle tested as it has been serving more than 40 million Internet requests per second for more than a few years."*

### Pitfalls / notes
- Migration was **risk-laden and deliberate**: they evaluated alternatives quarterly for years before committing (option-fatigue is real; "least resistance" delay has a cost).
- They had to build (not buy) an HTTP library to meet their needs — ecosystem gaps at the extreme scale end.
- ⚠️ Not verified in extracts: exact per-service cutover mechanics (graceful reload + gradual traffic shift is the documented mechanism in the framework docs).

---

## 3. Figma — TypeScript/Node multiplayer server → Rust (+ Rust file-representation work)

### What
Figma's multiplayer syncing server was rewritten from TypeScript/Node.js to Rust in 2019 ("Rust in production at Figma", Evan Wallace). Later posts document continued Rust optimization work (2022 memory optimizations).

### Why (drivers)
- **Unpredictable latency spikes from a single-threaded, GC'd runtime:** *"The main problem with the old server was the unpredictable latency spikes during syncing. The server was written in TypeScript and, being single-threaded, couldn't process operations in parallel. That meant a single slow operation would lock up the entire worker… operations would take an arbitrarily-long amount of time. Users connected to that worker would be unable to sync."* Plus explicit GC blame: *"Low resource usage was particularly important to us because some of the performance issues with the old server were caused by the garbage collector."* ([figma.com/blog/rust-in-production-at-figma](https://www.figma.com/blog/rust-in-production-at-figma))
- **Memory footprint as an architectural enabler:** *"It used so little memory that it was actually practical to just start a separate Rust process for every document."*

### Process
- **Scope discipline — port only the hot component:** *"we dropped our initial plan to rewrite our whole server in Rust and chose to focus solely on the performance-sensitive part instead."* The rest of the stack stayed on standard server languages.
- A 2020-era follow-up post, "An update on Rust in Figma's tech stack" (describing multithreading rationale), is frequently cited but **⚠️ UNVERIFIED — could not be located on figma.com via live search or extraction on 2026-10-25** (URL guesses and site-scoped searches returned nothing; likely conflated with the 2019 post above or removed/renamed). Do not cite without further checking.
- Continued evolution: data-representation work in the existing Rust server (2022): replaced a map with a vector for file state, plus 48-bit-pointer "bit stuffing".

### Results (quantified)
- 2022 post: *"it improved file deserialization times by 20% at the 99th percentile… And this small change also saved us 20% in memory costs across our entire multiplayer fleet."* Also: *"our memory usage… dropped almost 25% for large files"* ([figma.com/blog/supporting-faster-file-load-times-with-memory-optimizations-in-rust](https://www.figma.com/blog/supporting-faster-file-load-times-with-memory-optimizations-in-rust)).
- 2019 rewrite: the server could serve each document in its own process with predictable sync latency (no more worker-wide stalls). (Exact speedup factor not tabulated in the post.)

### Pitfalls (2019 post, verbatim list)
- *"Asynchronous Rust is difficult"* (2019-era async ecosystem immaturity).
- Library bugs forced an FFI fallback: *"We ended up just using a tried-and-true C library instead… Rust is built on LLVM so it's pretty trivial to call C code from Rust."*
- *"Errors are hard to debug"* — `Result`-based errors don't capture stack traces of the causing code.
- Language maturity overall: *"the language wasn't as ready as we thought… still has a lot of rough edges"* — the direct cause of the narrowed rewrite scope.

---

## 4. AWS / Amazon — Firecracker, Bottlerocket, S3/Mountpoint, guidance posts

### What
AWS builds infrastructure in Rust rather than wholesale "migrating" from Python, but their published material contains explicit migration guidance and several greenfield/replacement stories.

### Why (drivers, verified)
- From "Why AWS loves Rust" (2020): *"Because Rust does not require a runtime or garbage collector, it is able to achieve runtime performance similar to C and C++. At the same time, Rust uses a strict type system and ownership model to achieve compile-time verification of memory and concurrency safety, making the cost of testing and validating Rust implementations significantly lower than C/C++."* AWS's first notable Rust product, **Firecracker** (2018), *"provides the open source virtualization technology that powers AWS Lambda"*; Rust also in S3, EC2, CloudFront, Route 53, **Bottlerocket** (2020), and Nitro components ([aws.amazon.com/blogs/opensource/why-aws-loves-rust-and-how-wed-like-to-help](https://aws.amazon.com/blogs/opensource/why-aws-loves-rust-and-how-wed-like-to-help)).
- **Firecracker** ([github.com/firecracker-microvm/firecracker](https://github.com/firecracker-microvm/firecracker)): *"a virtual machine monitor (VMM) that uses KVM to create and run microVMs… It excludes unnecessary devices… to reduce the memory footprint and attack surface area… developed at Amazon Web Services to accelerate the speed and efficiency of services like AWS Lambda and AWS Fargate."* Written in Rust for C-level performance with memory safety (per AWS blog).
- **Bottlerocket** GA (2020): *"Large parts of Bottlerocket are written in Rust, a modern programming language that helps ensure thread safety and prevent memory-related errors, such as buffer overflows that can lead to security vulnerabilities."* Its build system is Rust-based (cargo-make, cargo-deny) ([aws.amazon.com/blogs/opensource/announcing-the-general-availability-of-bottlerocket-…](https://aws.amazon.com/blogs/opensource/announcing-the-general-availability-of-bottlerocket-an-open-source-linux-distribution-purpose-built-to-run-containers)).
- **S3 tooling:** Mountpoint for Amazon S3 — *"We use Rust because it offers reliability without compromise: we get strong safety guarantees like memory safety and a rich type system, without paying the cost of a language runtime or garbage collector… Mountpoint uses Rust to reduce tail latency and minimize resource usage."* Same post notes *"Teams across Amazon S3 and AWS are building core services in Rust today, including the ShardStore storage node"* ([aws.amazon.com/blogs/storage/the-inside-story-on-mountpoint-for-amazon-s3-a-high-performance-open-source-file-client](https://aws.amazon.com/blogs/storage/the-inside-story-on-mountpoint-for-amazon-s3-a-high-performance-open-source-file-client)).
- **Team/experience note (Prime Video):** *"Using Rust has enabled programmers of all experience levels to contribute code without requiring reviewers to carefully scrutinize every line for safety pitfalls. We trust the compiler, and we can focus our code reviews on functionality, not language corner cases."* ([amazon.science/blog/how-prime-video-updates-its-app-for-more-than-8-000-device-types](https://www.amazon.science/blog/how-prime-video-updates-its-app-for-more-than-8-000-device-types))
- **Recent (re:Invent 2025, "Unleash Rust's potential on AWS", DEV307)** — Amazon Aurora DSQL started as a Kotlin/JVM project; a Kotlin-developer team trialed Rust on one component (the Adjudicator), got *"10 times faster than a Kotlin implementation, despite zero attempts at optimization"*, then rewrote the whole control plane in Rust. On an ingest/query service side-by-side rollout: the previous-gen (Go) side *"was running at 60,000 points per second per pod, we started dropping queries"* while the Rust side *"jumped all the way up to 3.5 million requests per second"*. Policy quote: *"any data plane project in AWS right now, by default, uses Rust."* ([youtube.com/watch?v=buBBQ5mXAi8](https://www.youtube.com/watch?v=buBBQ5mXAi8) — conference transcript via Tavily; treat as secondary-source quotes)

### Process (what AWS documents about *how* to migrate)
- Component-level trial first (DSQL: one component, measure, then expand) — a strangler-fig pattern.
- ⚠️ UNVERIFIED: a standalone AWS post titled **"Why we're moving to Rust"** or **"How Amazon uses Rust"** (2024–2025), and a formal **"identifying candidate workloads for Rust"** guidance page describing *dark launches* and *side-by-side comparison*. Live searches on aws.amazon.com / amazon.science / aboutamazon.com / docs.aws.amazon.com on 2026-10-25 found no post with those exact titles; nearest verified equivalents are "Why AWS loves Rust" (2020), "Sustainability with Rust" (2021 — energy/carbon efficiency framing), the 2024 CodeArtifact-for-Cargo post (notes Rust topped Stack Overflow's "most desired language" for the 8th year, >80%), and the DEV307 talk above. The "side-by-side comparison" methodology *is* visible in the DSQL/DEV307 rollout graphs, but the specific "dark launch" guidance document could not be verified.

### Results / Pitfalls
- Verified quantified outcomes: Pingora-class figures above are Cloudflare's; for AWS the strongest public numbers are the DEV307 claims (10× vs Kotlin with zero optimization; 60k→3.5M req/s vs previous-gen Go service) — conference talk, not a written engineering post.
- AWS repeatedly notes Rust lowers validation cost vs C/C++ but also invested in **upstream ecosystem** (hired Tokio committers Carl Lerche, Sean McArthur, Lucio Franco, Jon Gjengset) — i.e., they treated ecosystem maturity as a cost they had to pay down themselves.

---

## 5. InfinyOn / Fluvio — "From Python to Rust"

### What (verified)
Fluvio is a Rust-based streaming platform. Verified InfinyOn/Fluvio posts relevant to Python↔Rust:
- *"How we built our Python Client that's mostly Rust"* (Sebastian Imlay) — Python bindings wrapped around the Rust core ("Learn how to wrap your Rust crate in Python") ([infinyon.com/blog](https://www.infinyon.com/blog)).
- "Introducing Fluvio" (June 2021): Rust chosen because *"Fluvio needs to perform stream processing at a massive scale"*, with performance/safety/deployment-footprint benefits (single ~37 MB binary, WASM-based smart modules) ([infinyon.com/blog/2021/06/introducing-fluvio](https://www.infinyon.com/blog/2021/06/introducing-fluvio)).

### ⚠️ UNVERIFIED
The specific post **"From Python to Rust"** describing a port of an ML/DSP pipeline (Python + NumPy/SciPy → Rust) could **not** be located on 2026-10-25: exact-phrase searches (advanced + `exact_match`), site-scoped searches (infinyon.com, medium.com, dev.to), and direct URL probing all failed (a guessed URL soft-404s to the InfinyOn homepage; the post is absent from the blog index and the `Rust` tag listings). The broader port-of-a-numeric-Python-pipeline narrative is consistent with the verified "Python Client that's mostly Rust" and Fluvio's Rust-first platform story, but **the specific post's reasons and pain points cannot be quoted** — treat its existence as unconfirmed until found in an archive (e.g., Wayback Machine capture of infinyon.com/blog 2020–2022).

---

## 6. Dropbox — Python sync engine → Rust ("Nucleus")

### What
Dropbox's desktop file-sync engine — the heart of the client since ~2008, originally written in Python ("Sync Engine Classic") — was rewritten from scratch in Rust ("Nucleus"), publicly described in "Rewriting the heart of our sync engine" (March 2016), with the rollout completing years later. Related Rust work: Brotli compression in Rust (2016), a custom Rust library for Capture (2021).

### Why (drivers)
- Correctness & change-safety over raw speed: *"We wrote Nucleus in Rust! Rust has been a force multiplier for our team, and betting on Rust was one of the best decisions we made. More than performance, its ergonomics and focus on correctness has helped us tame sync's complexity. We can encode complex invariants about our system in the type system and have the compiler check them for us."* ([dropbox.tech/infrastructure/rewriting-the-heart-of-our-sync-engine](https://dropbox.tech/infrastructure/rewriting-the-heart-of-our-sync-engine))
- The old Python system had a **permissive data model** and no testability: *"the system was not designed for testability. We relied on slow rollouts and debugging issues in the field rather than automated pre-release testing. Sync Engine Classic's permissive data model meant we couldn't check much in stress tests, since there were large sets of undesirable yet still legal outcomes we couldn't assert against. Having a strong data model with tight invariants is immensely valuable for testing."*
- Memory/perf angle (secondary, from Dropbox engineer Sujay Jayakar, Business Insider 2020): *"The benefits of Rust have been a huge, huge win for us… We've been able to move quickly. We don't need to spend as much time hunting down bugs."* ([businessinsider.com/what-is-rust-programming-language-amazon-facebook-discord-love-it-2020-6](https://www.businessinsider.com/what-is-rust-programming-language-amazon-facebook-discord-love-it-2020-6))

### Process (the richest public methodology of any case here)
- **Architecture for determinism first:** *"Almost all of our code runs on a single thread (the 'Control thread') and uses Rust's futures library for scheduling many concurrent actions on this single thread. We offload work to other threads only as needed: network IO to an event loop, computationally expensive work like hashing to a thread pool, and filesystem IO to a dedicated thread. This drastically reduces the scope and complexity developers must consider."* (primary post)
- **Deterministic simulation testing:** (Dropbox engineer via [mjtsai.com summary of the 2020 talk](https://mjtsai.com/blog/2020/03/17/rewriting-dropboxs-sync-engine-in-rust/)): *"all of these scheduling decisions are made psuedorandomly from a fixed RNG seed… once we find a failure, we're guaranteed that we can reproduce it given its original seed… we actually don't even log in CI at all. we run millions of seeds every day and then if CI finds a failure, it just prints the seed."* Implementation recipe: single-threaded logic with fixed inputs; traits at every thread boundary; mocked external time; queue-pausing mock components.
- **Sober rewrite discipline:** *"Rewriting the sync engine was really hard, and we don't want to blindly celebrate it, because in many environments it would have been a terrible idea. It turned out that this was an excellent idea for Dropbox but only because we were very thoughtful about how we went about this process… like having a very clean data model."* (mjtsai quoting the talk)
- **Team fluency:** from a Dropbox engineer's retrospective ([news.convex.dev/a-tale-of-three-codebases](https://news.convex.dev/a-tale-of-three-codebases)): *"With Nucleus… we made sure that the entire team was fluent and actively contributing to the codebase. Within the Nucleus team, Rust was an unequivocal success."*
- Related Python↔Rust interop precedent: Brotli ported to Rust for the storage backend (Jun 2016, [dropbox.tech/infrastructure/lossless-compression-with-brotli](https://dropbox.tech/infrastructure/lossless-compression-with-brotli), per the site's Rust tag page), and "Why we built a custom Rust library for Capture" (Sep 2021).
- ⚠️ UNVERIFIED: a Dropbox post titled **"Open Sourcing Our Rust Libraries"** — the live Dropbox.tech open-source and Rust tag listings on 2026-10-25 show no such post (only the Go libraries post from 2018). Do not cite it.

### Results
- No public p99/CPU tables; documented outcomes are reliability/velocity qualitative ("unequivocal success", compiler-checked invariants, deterministic reproducibility), plus scale claims from third-party summaries (Nucleus *"runs on over half a billion devices"* — convex.dev; treat as secondary). Rollout was incremental and multi-year.

### Pitfalls
- From the same Dropbox retrospective (convex.dev):
  - **Developer fungibility breaks:** *"Rust is a hard language to learn… The first time a developer runs afoul of the borrow checker… requires some actual dedicated learning time."*
  - **Organizational language churn:** *"Perhaps one of the biggest downsides from introducing new languages isn't that it requires more work on the new codebases but that it detracts from investment in existing codebases. We switched Dropbox's backend stack from Python to Go and later switched some high profile projects from Go to Rust… As senior engineers switched to these exciting new projects however there was less cutting-edge work done on the existing codebases."* ("More wood behind fewer arrows.")
  - Success required **whole-team fluency** and relative isolation of the Rust component (desktop client ≠ backend Python/Go fleet).

---

## 7. Other well-documented cases

### npm (Node.js → Rust, CPU-bound registry service) — 2019 Rust whitepaper
- **Driver:** a publish-path service was *"a CPU-bound task that was projected to become a performance bottleneck"*; *"The legacy JavaScript implementation of this service in Node.js was in need of a rewrite in any case."* Scale: *"1.3 billion package downloads per day"*, *"30 billion"* monthly (2019).
- **Language selection process:** *"the team quickly rejected using C, C++, and Java, and took a close look at Go and Rust."* Security memory-management concern for C-family: *"I wouldn't trust myself to write a C++ HTTP application and expose it to the web"* — Chris Dickinson. Chose Rust.
- **Pitfalls (verbatim):** *"the maintenance burden of having separate solutions for monitoring, logging, and alerting for the existing JavaScript stack as well as the new Rust stack. As a young language, Rust doesn't yet have industry-standard libraries and best practices."* Plus: Rust *"front-loads decisions about memory usage"*; harder to grapple with, but *"when the engineers encountered problems, the Rust community was helpful and friendly."*
- **Outcome:** reimplemented and deployed to production; conclusion: *"Rust is a solution that scales and is straightforward to deploy. It keeps resource usage low without the possibility of compromising memory safety. Dependency management through Cargo brings modern tools to the systems programming domain."* ([rust-lang.org/static/pdfs/Rust-npm-Whitepaper.pdf](https://rust-lang.org/static/pdfs/Rust-npm-Whitepaper.pdf))
- **Note:** npm is JavaScript→Rust, not Python→Rust — but the "one hot, CPU-bound service, JS/Python everywhere else, dual-stack observability cost" pattern is exactly the Python case shape.

### 1Password (full product-line rewrite around a Rust core) — Serokell interview
- **Driver:** *"One of the main things that drew us to Rust initially was the memory safety… maximize our confidence in the safety of our customers' secrets."* Plus *"a significant performance benefit to the lack of a traditional runtime; we don't have to worry about the overhead of a garbage collector"* ([serokell.io/blog/rust-in-production-1password](https://serokell.io/blog/rust-in-production-1password)).
- **Process:** headless Rust core containing *"all of the business logic, cryptography, database access, server communication"* wrapped in *"a thin UI layer that is native to the system."*
- **Pitfalls:** ecosystem gaps in security-sensitive niches — *"we did have to roll our own logging and tracing tools… we constructed a substantial localization implementation."* Mitigation patterns: RustSec database audits in CI, Cargo's test framework.

### Amazon Aurora DSQL (Kotlin/JVM → Rust) — see §4 (re:Invent 2025 DEV307): component trial (Adjudicator, 10× faster with zero optimization) → whole-system rewrite; new default *"any data plane project in AWS… uses Rust."*

### Practitioner synthesis guide (Cloudsmith, via corrode.dev)
- Relevant validated advice for Python→Rust programs: *"Prioritize CPU-Bound Tasks First — Start with computationally intensive operations that don't require heavy I/O"*; *"You don't have to migrate your entire data science stack to Rust… You can start by using the Rust libraries for data preprocessing and ETL tasks. They have Python bindings, so you get a lot of the benefits of Rust without having to do a full migration."* ([corrode.dev/learn/migration-guides/python-to-rust](https://corrode.dev/learn/migration-guides/python-to-rust))

---

## Lessons that apply to an automated Python→Rust framework

1. **Port hot paths, not systems.** Every successful migration here was scoped to a measured bottleneck: one service (Discord Read States), one data structure (SortedSet NIF), one server component (Figma multiplayer), one CPU-bound service (npm). Figma explicitly abandoned a full-server rewrite when the language proved less ready than expected. An automated framework should select functions/modules by profile evidence (allocation rate, GC scan cost, CPU time) — not translate whole programs.
2. **The dominant driver is tail latency via GC scan cost, not average speed.** Discord's Go spikes came from the GC scanning a *large live working set* (the LRU cache) even with *"very few allocations"*; Figma blamed its TS GC; 1Password cited GC overhead. Python's GC + reference-counting has the same shape (cost proportional to object-graph size; cache/registry objects amplify it). A framework can *predict* winners: large long-lived object graphs (big caches, registries, meshes/graphs) → high GC-scan cost → high Rust payoff.
3. **Data-structure change can dominate language change.** Figma's vector-instead-of-map swap alone gave 20% p99 deserialization and ~20–25% memory; Discord's LRU partitioning changed spike size before any language change. A literal transliteration of bad Python data structures forfeits much of the win; the framework should treat data-representation redesign as a first-class, expected transformation (and validate it separately).
4. **Differential validation is the project-critical machinery — and it's automatable.** Verified patterns: (a) load-test + canary node + fix edge cases + fleet (Discord); (b) side-by-side production metrics old-vs-new (Discord graphs, AWS DSQL rollout curves); (c) deterministic simulation with seeded schedules — *"we run millions of seeds every day and then if CI finds a failure, it just prints the seed"* (Dropbox); (d) tight-invariant data models that make state assertions possible (Dropbox). An automated framework should generate: golden-input corpora, old-vs-new output differ (a shadow harness), property/invariant checks from inferred data models, and fuzz/load harnesses — this is where these teams spent most of their effort.
5. **Interoperability is the adoption path.** Discord used Rust NIFs inside Elixir; Dropbox kept Python tooling around a Rust core and shipped Rust→Python bindings at InfinyOn; 1Password wrapped a headless Rust core in native UIs; corrode/Cloudsmith recommend Python bindings for ETL/data-prep first. For Python→Rust: generate a PyO3/wheel wrapper so the Rust port is a drop-in module, keeping Python orchestration intact — each company's story confirms "Rust where hot, high-level language everywhere else" as the stable equilibrium (Discord: *"keep our core real-time communications logic in the higher-level Elixir… dropping down into Rust when needed"*).
6. **Concurrency architecture should be simplified, not translated.** Dropbox's single Control thread + futures + dedicated IO/hash threads, Figma's one-Rust-process-per-document, Pingora's explicit multi-threading with graceful reload: the winners reduced concurrency scope before/while porting. An automated framework should prefer ports whose structure maps to a single-threaded core + narrow, trait-bounded async boundaries (also what makes deterministic testing possible).
7. **Expect Rust to beat hand-tuned GC-language code with less tuning** — *"Even with just basic optimization, Rust was able to outperform the hyper hand-tuned Go version"* (Discord); Aurora DSQL's *"10 times faster… despite zero attempts at optimization"* (⚠️ conference talk). Corollary: don't spend automation effort on micro-optimization; spend it on correctness and structure.
8. **Budget for ecosystem gaps and dual-stack costs.** Documented: Figma fell back to a C library via FFI and struggled with async Rust; npm paid for two monitoring/logging/alerting stacks; 1Password wrote their own logging/tracing/localization; Cloudflare wrote their own HTTP library. For a framework: include FFI fallback generation, and treat cross-language observability (single telemetry plane over both Python and Rust) as a deliverable.
9. **Human factors are the real constraint.** Whole-team fluency was required for success (Dropbox Nucleus: *"entire team was fluent"* → *"unequivocal success"*); borrow-checker literacy needs *"dedicated learning time"*; language churn starved legacy codebases ("more wood behind fewer arrows"). An *automated* framework inverts some of this (no borrow-checker ramp for the porting team), but review/maintenance still needs Rust-literate humans — and the org cost of splitting attention between the Python original and Rust port remains.
10. **Rewrites deserve humility.** Dropbox: *"in many environments it would have been a terrible idea… only because we were very thoughtful."* Criteria extracted from the cases that justify a rewrite: measured bottleneck with structural cause (GC scan, single-thread lock-up, projected CPU bottleneck); narrow replaceable scope; testable/deterministic boundaries; team ownership; and a validated rollback/canary path. An automated framework should implement these as gating checks before generating any Rust.

### Verification notes (read before citing)
- **⚠️ Unverified / not found (2026-10-25):** Figma "An update on Rust in Figma's tech stack"; InfinyOn "From Python to Rust" (ML/DSP pipeline); Dropbox "Open Sourcing Our Rust Libraries"; AWS posts titled exactly "Why we're moving to Rust" / "How Amazon uses Rust" (2024–2025); AWS "identifying candidate workloads for Rust" with "dark launch" language. Discord "shadow testing" (post documents canary + load testing instead). Aurora DSQL figures come from a 2025 re:Invent talk transcript, not a written AWS post.
- All other quoted text was extracted verbatim from the live URLs listed in Sources on 2026-10-25.

---

## Sources

**Primary (full text extracted 2026-10-25):**
1. Discord — Why Discord is switching from Go to Rust (2020): https://discord.com/blog/why-discord-is-switching-from-go-to-rust
2. Discord — How Discord Scaled Elixir to 5,000,000 Concurrent Users (2017): https://discord.com/blog/how-discord-scaled-elixir-to-5-000-000-concurrent-users
3. Discord — Using Rust to Scale Elixir for 11 Million Concurrent Users (2020): https://discord.com/blog/using-rust-to-scale-elixir-for-11-million-concurrent-users
4. Cloudflare — How we built Pingora (2022): https://blog.cloudflare.com/how-we-built-pingora-the-proxy-that-connects-cloudflare-to-the-internet/
5. Cloudflare — Pingora open source announcement (2024): https://blog.cloudflare.com/pingora-open-source/ (open-sourcing corroborated via https://github.com/cloudflare/pingora README and https://www.phoronix.com/news/CloudFlare-Pingora-No-Nginx)
6. Figma — Rust in production at Figma (2019): https://www.figma.com/blog/rust-in-production-at-figma/
7. Figma — Supporting faster file load times with memory optimizations in Rust (2022): https://www.figma.com/blog/supporting-faster-file-load-times-with-memory-optimizations-in-rust/
8. AWS — Why AWS loves Rust (2020): https://aws.amazon.com/blogs/opensource/why-aws-loves-rust-and-how-wed-like-to-help/
9. AWS — Bottlerocket GA announcement (2020): https://aws.amazon.com/blogs/opensource/announcing-the-general-availability-of-bottlerocket-an-open-source-linux-distribution-purpose-built-to-run-containers
10. Dropbox — Rewriting the heart of our sync engine (2016): https://dropbox.tech/infrastructure/rewriting-the-heart-of-our-sync-engine
11. npm — Rust npm Whitepaper (2019): https://rust-lang.org/static/pdfs/Rust-npm-Whitepaper.pdf
12. 1Password — Rust in Production: 1Password (Serokell): https://serokell.io/blog/rust-in-production-1password

**Secondary (search-verified snippets / corroborating):**
13. Cloudflare Pingora GitHub (crate list, features, scale): https://github.com/cloudflare/pingora
14. Firecracker GitHub: https://github.com/firecracker-microvm/firecracker
15. AWS Storage Blog — Mountpoint for Amazon S3 (Rust rationale, ShardStore): https://aws.amazon.com/blogs/storage/the-inside-story-on-mountpoint-for-amazon-s3-a-high-performance-open-source-file-client
16. Elixir-lang blog — Real-time communication at scale with Elixir at Discord (2020): https://elixir-lang.org/blog/2020/10/08/real-time-communication-at-scale-with-elixir-at-discord
17. Michael Tsai — Rewriting Dropbox's Sync Engine in Rust (2020, quotes Dropbox talk on deterministic testing): https://mjtsai.com/blog/2020/03/17/rewriting-dropboxs-sync-engine-in-rust/
18. A Tale of Three Rust Codebases (Dropbox retrospective: team fluency, org costs): https://news.convex.dev/a-tale-of-three-codebases
19. Dropbox.tech Rust tag page (verifies which Rust posts exist; "Open Sourcing Our Rust Libraries" absent): https://dropbox.tech/tag-results.rust
20. Dropbox Brotli in Rust (2016): https://dropbox.tech/infrastructure/lossless-compression-with-brotli
21. Dropbox custom Rust library for Capture (2021): https://dropbox.tech/application/why-we-built-a-custom-rust-library-for-capture
22. Amazon Science — Prime Video (Rust and code review trust): https://www.amazon.science/blog/how-prime-video-updates-its-app-for-more-than-8-000-device-types
23. AWS re:Invent 2025 DEV307 "Unleash Rust's potential on AWS" (Aurora DSQL Kotlin→Rust; side-by-side rollout numbers): https://www.youtube.com/watch?v=buBBQ5mXAi8
24. Business Insider — Why Amazon, Facebook, Discord love Rust (Sujay Jayakar quotes): https://www.businessinsider.com/what-is-rust-programming-language-amazon-facebook-discord-love-it-2020-6
25. corrode.dev — Migrating from Python to Rust (practitioner guide, Cloudsmith case): https://corrode.dev/learn/migration-guides/python-to-rust
26. InfinyOn blog index ("How we built our Python Client that's mostly Rust"; "From Python to Rust" absent): https://www.infinyon.com/blog
27. InfinyOn — Introducing Fluvio (2021, Rust rationale): https://www.infinyon.com/blog/2021/06/introducing-fluvio
28. AWS News Blog — CodeArtifact adds Cargo support (2024, Rust "most desired" 8th year): https://aws.amazon.com/blogs/aws/aws-codeartifact-adds-support-for-rust-packages-with-cargo
