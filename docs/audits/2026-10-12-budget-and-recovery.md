# October 12 AWS budget and guarded recovery update

The owner explicitly approved the previously proposed single recovery/cutover
attempt up to $1 and clarified the new limit as **$40 total AWS spending per
calendar month**, including the existing server, storage and backups. The $1
belongs inside that total. The previous zero-increment restriction is superseded;
real-money trading and weakened operational/risk/resource gates remain outside
the authorization. Actual owner-answer evidence is retained privately by hash.

The prepared paid policy preserves a false no-increment-spend claim, one paid
attempt and the full allowance reservation after success, failure or ambiguous
responses. Its ledger survives deployment identity changes and UTC month
rollover. Reusing the same owner-answer hash cannot authorize a retry. Fresh
whole-account baseline cost upper estimates must cover every month of retained
storage, and each baseline plus the whole attempt allowance must fit $40.
Missing, stale, non-integer, excessive or next-month-omitting estimates refuse.

Existing S3 lifecycle controls expire current snapshots after 35 days and
noncurrent versions after seven more days. They do not expire incomplete
multipart uploads. The proposed paid implementation therefore compresses
directly into at most two parts of at most 4 GiB each, aggregate at most 8 GiB,
and uploads parts and manifest using conditional single PUTs with automatic SDK
retries disabled. A failed upload may leave completed lifecycle-covered objects
within the one-attempt byte allowance, without an indefinite multipart tail.
The manifest binds ordered hashes, sizes, encryption and version identities;
the independent streamed restore still verifies whole-gzip/raw digests, full
SQLite integrity/foreign keys and unchanged source lineage. IAM, lifecycle,
journal retention and native compute limits remain unchanged.

The October 12, 20:29 PDT read-only production snapshot still showed clean
September 4 source, exec-v4, no V7 weather-vintage tables, both original active
paper profiles, real execution off/dry-run on and no failed units. A later
22:02 PDT infrastructure read confirmed the existing ARM t4g.medium with about
4 GiB RAM and a 64-GB provisioned disk; no cloud configuration was changed.

The last verified monthly forecast was $54.50 on October 5. It is dated evidence,
not a current forecast or proof that the new $40 limit fits. The current console
session expired, and its saved-login attempt was not accepted. Fresh owner
sign-in is pending. No billing policy, cheap instance or projected savings is
invented to make the cap appear satisfied. No paid upload, full independent
restore, history import or V7 deployment has been performed.

The local research worker has completed successful scheduled batches, including
October 12 at 16:17 PDT. The 20:17 batch completed twelve new local weather
vintages, then failed while sending SIGSTOP for CPU pacing of its live ML child.
Prior successful analysis remains authoritative; partial output is not a full
successful run or recovery proof. The unresolved native supervision failure and
current competing-load admission deferral remain explicit. The same schedule is
temporarily unloaded during source maintenance and must be restored after a
clean commit. No resource or power gate is relaxed.

Focused validation of the proposed source passed 233 tests (recovery and
deployment-shell behavior), bytecode compilation and diff checks. Full required
CI on the final committed candidate remains a deployment gate. Fixture tests
prove neither real restore runtime nor actual monthly budget feasibility.
