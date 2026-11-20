CREATE TRIGGER resolution_links_gate_required BEFORE INSERT ON resolution_links
WHEN NOT EXISTS(
  SELECT 1 FROM gate_links g JOIN probe_results p USING(probe_run_id)
  WHERE g.probe_run_id=NEW.probe_run_id AND g.gate_receipt_ref=NEW.gate_receipt_ref
  AND g.checked=1 AND g.decision='approved' AND p.outcome='pass'
)
BEGIN
  SELECT RAISE(ABORT,'Resolution requires an independently checked gate');
END;
CREATE TRIGGER comparison_resolution_insert BEFORE INSERT ON run_comparison_items
WHEN (NEW.category='resolved' OR NEW.lifecycle_status_after='resolved') AND NOT EXISTS(
  SELECT 1 FROM resolution_links r WHERE r.finding_fingerprint=NEW.finding_fingerprint
  AND r.gate_receipt_ref=NEW.linked_gate_receipt_ref
)
BEGIN
  SELECT RAISE(ABORT,'Comparison resolution requires fingerprint-bound proof');
END;
CREATE TRIGGER comparison_resolution_update BEFORE UPDATE ON run_comparison_items
WHEN (NEW.category='resolved' OR NEW.lifecycle_status_after='resolved') AND NOT EXISTS(
  SELECT 1 FROM resolution_links r WHERE r.finding_fingerprint=NEW.finding_fingerprint
  AND r.gate_receipt_ref=NEW.linked_gate_receipt_ref
)
BEGIN
  SELECT RAISE(ABORT,'Comparison resolution requires fingerprint-bound proof');
END;
