\set ON_ERROR_STOP on

-- Load extension
CREATE EXTENSION IF NOT EXISTS system_stats;

-- OS information
SELECT * FROM pg_sys_os_info();

-- CPU information
SELECT * FROM pg_sys_cpu_info();

-- CPU usage information
SELECT * FROM pg_sys_cpu_usage_info();

-- Memory information
SELECT * FROM pg_sys_memory_info();

-- IO analysis information
SELECT * FROM pg_sys_io_analysis_info();

-- Disk information
SELECT * FROM pg_sys_disk_info();

-- Load average information
SELECT * FROM pg_sys_load_avg_info();

-- Process information
SELECT * FROM pg_sys_process_info();

-- Network information
SELECT * FROM pg_sys_network_info();

-- CPU + Memory by process
SELECT * FROM pg_sys_cpu_memory_by_process();

