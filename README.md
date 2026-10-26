# Reusable Oracle managed backup migration companion

## Disclaimer: supplementary bulk migration utility

This is an independently developed supplementary utility built specifically to orchestrate bulk migration of database backup objects. It is not MV2BUCKET, an Oracle-listed tool, an official MV2BUCKET extension, or an Oracle-supported product. No Oracle endorsement or support commitment is implied.

MV2BUCKET and OCI CLI are separate Oracle tools and must be obtained through their applicable distribution channels. This companion leaves the native MV2BUCKET executable unchanged and uses the installed tools to perform source access and destination transfers.

The companion is provided as-is, without warranty. Review the code, protect credentials, and validate it with a representative pilot before using it in a new environment. Successful object migration does not replace a database restore and recovery test.

## Overview

This package migrates a captured Oracle managed-backup inventory through bounded local staging to an explicitly configured OCI-compatible Object Storage destination. MV2BUCKET handles source LIST and GET. OCI CLI handles the destination. The native MV2 executable is unchanged and distributed separately.

Destination routing does not depend on a realm list in this companion. Configure the actual HTTPS service endpoint, region, namespace, bucket, authentication profile and optional CA bundle. Each environment still requires working source access, destination authorization, network connectivity and a live pilot. This package does not bypass realm isolation or establish support for every endpoint.

## Package contents

| File | Purpose |
| --- | --- |
| `migration_config.py` | Strict explicit job configuration and command construction |
| `inventory.py` | Complete MV2 table and OCI JSON parsing, size bounds and comparison |
| `mv2_plan.py` | Capture inventories, create bounded plans and reconcile snapshots |
| `mv2_plan_adapter.py` | Trusted adapter for validating and loading data-only manifests |
| `mv2_bulk.py` | Preview, transfer, resume, verify and clean owned local batches |
| `examples/` | Synthetic configuration examples with non-routable placeholder domains |
| `tests/` | Offline tests using temporary files and simulated tools |
| `BLOG.md` | Detailed problem statement, solution and case study |
| `PROVENANCE.txt` | Relationship to the earlier deployment and validation boundaries |

This is a new reusable package. Its planner and adapter are new implementations, rather than recovered copies of the original server’s custom planner. Keep it separate from existing production jobs and state.

## Prerequisites

Use a Linux migration host with MV2BUCKET and OCI CLI installed separately. Supply the existing source database home, OPC file and OPC authentication wallet. Use a supported Python runtime where available; the source retains Python 3.6-compatible syntax for legacy-host compatibility.

The migration user needs permission to read the source configuration, execute both tools, write the dedicated plan/state/stage directories and access the managed backup source. Some MV2 installations require root. Select an operating account appropriate to the installed Oracle tooling.

The target identity must have the permissions required for object listing and upload in the selected destination. API-key credentials must belong to the destination environment. `instance_principal` is an optional authentication mode, subject to that environment’s trust and installed client behavior. Version 1 requires an explicit OCI configuration file and profile for either mode; no keys are embedded in the job JSON.

This version uses single-part uploads and whole-file MD5 comparison. The configured exclusive batch cap cannot exceed 50 GiB. An individual object must fit below the batch cap. Larger objects need a separately designed multipart verification workflow.

## Configure one job

Copy `examples/job.alloy.json` or `examples/job.custom-realm.json` to a protected operator-controlled configuration directory and replace every placeholder. All example domains end in `.invalid`; they are not live destinations.

- Use the absolute paths of the actual MV2 and OCI executables. A bare `oci` name is not a PATH lookup in the configuration loader.
- Supply the destination’s exact HTTPS origin. Do not use a PAR URL or include credentials, an object path, a query or a fragment.
- Choose a new dedicated `plan_dir`, `stage_root` and `state_dir`. They must be separate directories, with none containing another.
- Keep tools, the job JSON, OPC files, wallets, the destination OCI configuration and CA files outside staging and state.
- `batch_bytes` is an exclusive upper bound. The default is decimal 25 GB; `reserve_bytes` defaults in the example to decimal 5 GB.
- The default example uses four source download workers and eight upload workers. The configured ranges are download 1–16 and upload 1–32.
- The configured UTF-8 locale must exist on the host. The original host supported `C.UTF-8`.

Paths may be absolute or relative to the job JSON directory. Shell variables are not expanded. The companion uses explicit target command options and removes inherited `OCI_CLI_*` settings before launching tools, so an old SSH session’s endpoint or profile does not select a different destination.

If authentication discovery needs region metadata unknown to the installed SDK, configure that SDK through its supported mechanism and test the actual client. An explicit object-storage endpoint alone does not configure every authentication service.

## Run offline validation

From the package directory:

```bash
python3 -m unittest discover -s tests -v
```

These tests do not contact Oracle or Alloy and do not require real backup data. `VALIDATION.json` records the runtime used and the actual test result for this distribution. Syntax compatibility is distinct from a full execution test under Python 3.6.

## Capture and plan

The following examples assume the job is `/secure/jobs/migration.json` and an existing private evidence directory is `/var/lib/mv2-evidence/job1`. Create the private evidence directory and the parent of the configured plan directory with the required ownership and space before capture/build. Keep evidence outside the stage and outside a not-yet-created plan directory.

Capture the complete source and destination inventories:

```bash
python3 mv2_plan.py --config /secure/jobs/migration.json capture-source --out /var/lib/mv2-evidence/job1/source.txt
python3 mv2_plan.py --config /secure/jobs/migration.json capture-target --out /var/lib/mv2-evidence/job1/target.json
```

The captures retain the native output, successful exit information, UTC timestamps, configuration binding and SHA-256 provenance. Destination capture uses all pages and no delimiter or object prefix. The source capture uses explicit OPC settings and no source filter.

Build a new plan at the configured `paths.plan_dir`:

```bash
python3 mv2_plan.py --config /secure/jobs/migration.json build --source-inventory /var/lib/mv2-evidence/job1/source.txt
```

The planner refuses to overwrite a plan. It conservatively budgets the rounded source size displays, validates literal names and selects bounded filter scopes. If a safe selector or budget cannot represent the requested set, planning stops.

The normal path plans the complete source snapshot. An explicit `--exclude-target-present` build with a captured `--target-inventory` can reduce catch-up downloads. Such exclusions establish name presence and size compatibility, not source-content checksum verification for excluded objects. Retain and review that distinction in the reconciliation report.

Capture sidecars are required by default. `--allow-external-inventory` is an explicit import option for previously exported inventories; those inputs are labelled as unverified imports. Live source preflight still runs before transfer.

## Preview and transfer a pilot

```bash
python3 mv2_bulk.py --config /secure/jobs/migration.json preview 1
python3 mv2_bulk.py --config /secure/jobs/migration.json run 1
python3 mv2_bulk.py --config /secure/jobs/migration.json cleanup 1
```

Preview reads source and destination metadata and writes local diagnostic state. It does not download, upload or delete backup objects. Review the selected source names, target context, estimated size and free-space requirement.

The pilot downloads its whole selected source batch, checks the exact staged set, computes local byte sizes and MD5 values, compares existing target objects, uploads missing names and rechecks the destination. A successful run writes a verified receipt and retains staging by default. Cleanup rechecks the receipt, destination and owned local staged bytes before removing only that batch directory.

Use a representative pilot for each distinct destination, authentication mode and CA configuration. Confirm the installed CLI exposes the required options and that real uploaded objects have a comparable whole-file MD5. Offline endpoint tests do not replace this pilot.

## Run the remaining range

After reviewing the pilot, use the appropriate remaining start batch. This example is for Bash:

```bash
nohup python3 -u mv2_bulk.py --config /secure/jobs/migration.json run-range --start 2 --cleanup-verified > /var/lib/mv2-evidence/job1/migration.log 2>&1 < /dev/null & disown
```

Omitting `--end` processes the rest of the fixed plan. The range stops at the first failure. It does not poll the source forever or include backups created after that plan’s snapshot. A receipt is rechecked against the destination before a completed batch is skipped.

No environment exports need to be restored after a new SSH session. Run with the same protected job JSON and review the saved state. If the context changes, create a separate job instead of reusing receipts against a new endpoint or database.

## Reconcile after transfer

Capture fresh source and target inventories into new files, then compare them:

```bash
python3 mv2_plan.py --config /secure/jobs/migration.json capture-source --out /var/lib/mv2-evidence/job1/source-final.txt
python3 mv2_plan.py --config /secure/jobs/migration.json capture-target --out /var/lib/mv2-evidence/job1/target-final.json
python3 mv2_plan.py --config /secure/jobs/migration.json reconcile --source-inventory /var/lib/mv2-evidence/job1/source-final.txt --target-inventory /var/lib/mv2-evidence/job1/target-final.json --out /var/lib/mv2-evidence/job1/reconciliation
```

The reconciliation compares the currently listed source name set with the complete destination, reports missing names, size-bound differences and extra destination objects, and preserves input provenance. It is an inventory comparison, not a live source-content hash test.

Use `--original-plan /path/to/original/plan.json` when reconciling the historical plan as well. Preserve unavailable original names in the exception report. A later inventory must not silently erase the earlier obligation. Investigate those names separately; automatic deletion, its actor and its timestamp cannot be inferred just from absence.

Source and target listings are not an atomic snapshot. Coordinate a cutoff or capture source inventories around the target listing and compare them before customer sign-off. Mutable metadata or wallet backups can change contents without changing their names or rounded size displays.

## Failure and retry behavior

The companion never deletes source or target objects and never overwrites a conflicting destination object. Missing or non-comparable MD5 values, source drift, an unexpected stage file, an unsafe path, a failed subprocess or a receipt-context change stop the batch and retain evidence.

GET incremental behavior refers to local staging. It is not destination-aware source selection. The complete selected batch is downloaded unless a matching successful-download receipt permits validated local reuse. Partial files without that receipt may be incomplete. Preserve them and the earlier evidence, then create a separate retry job with new plan, stage and state directories, fresh inventory captures and enough space. The plan and captures are bound to the complete job configuration, so changing only the stage/state paths does not authorize reuse of an old plan.

Stage and state locks are local locks, not distributed coordination. Other scripts and schedulers must be coordinated by the operator. Sampled disk checks can overshoot between checks; use disk-backed staging and an appropriate reserve.

If credentials, configuration or code change, receipt reuse can be refused intentionally. Preserve the old context and evidence rather than editing a receipt to make it match.

## Acceptance and evidence

For a new environment, retain the successful pilot, per-batch receipts, final inventories, reconciliation output and any historical exception ledger. Report exactly which objects have checksum evidence. Existing-name exclusions and aggregate bucket totals do not establish source-content equality.

Object-copy integrity and database recoverability are separate acceptance criteria. A restore/recovery test requires the intended backup chain, metadata, keys and database tooling. The companion does not perform that test.

## References

- [OCI CLI global options](https://docs.oracle.com/en-us/iaas/tools/oci-cli/latest/oci_cli_docs/oci.html)
- [OCI CLI bulk upload](https://docs.oracle.com/en-us/iaas/tools/oci-cli/latest/oci_cli_docs/cmdref/os/object/bulk-upload.html)
- [OCI CLI complete object listing](https://docs.oracle.com/en-us/iaas/tools/oci-cli/latest/oci_cli_docs/cmdref/os/object/list.html)
- [OCI configuration](https://docs.oracle.com/en-us/iaas/Content/API/Concepts/sdkconfig.htm)
- [Adding region metadata](https://docs.oracle.com/en-us/iaas/Content/API/Concepts/sdk_adding_new_region_endpoints.htm)

Read `PROVENANCE.txt` for what was observed in the original case and what is new in this reusable package.
