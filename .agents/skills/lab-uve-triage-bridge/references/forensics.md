# Live forensic acquisition and UVE

Use this reference when collecting disk health, operating-system logs, crash dumps or other live-machine evidence. The engine currently has a generic command adapter, not a built-in physical-disk acquisition or disk-health certification adapter.

## Preserve acquisition scope

Bind the actual source by model, stable serial/EUI, volume and partition offset. Disk numbers can change after reboot. Save new evidence on a different physical device where possible. Record collector, command/tool versions, privileges, local time zone, acquisition start/end, errors and size limits.

A running mounted system changes while it is observed. Identify live logical collection as such; do not call it an offline sector image or claim write blocking. Keep raw exported logs and original dump copies alongside derived JSON/text. Record source/copy SHA-256 and metadata stability. A full memory dump header is only a header. A partial journal does not reconstruct events outside its retained window.

Separate reading evidence from repairs and stress tests. The diagnostic workflow does not require CHKDSK repair, self-tests, benchmarks, firmware changes or repeated reads of a known failing file. Preserve unavailable sections as gaps. Administrative access may be needed for protected logs or dumps; use the normal authorized operating-system mechanism and retain the acquisition's actual privilege level.

## Interpret storage evidence carefully

- NVMe media-error counters count detected error occurrences, not unique files or physical NAND blocks. Windows I/O events and controller SMART counters are separate measurements.
- Compare actual LBA ranges and transfer sizes. Larger overlapping reads and repeated reads of one range can produce many posts from one local problem.
- Establish file/volume mappings from real partition, sector and cluster metadata. Keep disk offsets, volume offsets and file offsets distinct.
- Separate media read errors, timeouts/resets, unsupported command responses, low free space and filesystem structure findings. Do not add different layers' posts as independent physical failures.
- A stable counter over a short interval and a clean filesystem dirty flag do not establish full content integrity. Without an earlier SMART snapshot, old Windows posts cannot reconstruct the historical counter exactly.
- Analyze acquired crash dumps with the platform's debugger and matching symbols. A driver on a reset/wait stack identifies an observed path, not automatically the defective physical component. Preserve partial symbol or minidump coverage.

## Verify frozen evidence

Use a task-scoped `command_check` that checks acquisition-time hashes, device identity, raw/derived consistency and the exact reproducible assertions. Bind the acquisition, specification, verifier and policies in the profile inventory. If size budgets do not cover an input, use supported explicit identity binding or a scoped budget; record the choice rather than dropping the file.

Keep two outcomes separate:

1. Evidence integrity and reproducibility of the declared observation.
2. Device health, integrity of all user files, repaired application behavior, or exact component root cause.

Approval of the first does not approve the second. Report missing physical imaging, trusted comparison hashes, hardware isolation or independent assessment as `not_verified` for those capabilities. Bundle-schema approval proves only the validator's declared contract.

Do not publish actual event logs, journals, dumps, device identifiers, private paths or raw incident packages as reusable code examples. Publish the workflow or sanitized synthetic fixtures instead.
