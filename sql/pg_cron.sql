-- Schedule a job (run every minute)
SELECT cron.schedule('test_job', '* * * * *', 'SELECT 1');

-- Schedule vacuum daily at 3:30 AM
SELECT cron.schedule('nightly_vacuum', '30 3 * * *', 'VACUUM');

-- Schedule cleanup every day at noon
SELECT cron.schedule('cleanup_old_data', '0 12 * * *',
  $$DELETE FROM logs WHERE created_at < NOW() - INTERVAL '30 days'$$);

-- View all scheduled jobs
SELECT * FROM cron.job;

-- View job run history
SELECT * FROM cron.job_run_details ORDER BY start_time DESC LIMIT 10;

-- Unschedule a job by name
SELECT cron.unschedule('test_job');


-- Cleanup old job details
DELETE FROM cron.job_run_details WHERE end_time < now() - interval '7 days';