UPDATE run_summaries SET project_id=(SELECT r.project_id FROM runs r WHERE r.run_id=run_summaries.run_id)
WHERE project_id!=(SELECT r.project_id FROM runs r WHERE r.run_id=run_summaries.run_id);
