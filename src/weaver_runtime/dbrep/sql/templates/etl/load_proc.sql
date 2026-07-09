create or alter procedure $load_procedure
as
begin
    set nocount on;
    declare @weaver_load_datetime datetime2(6) = sysutcdatetime();

$start_artifact_cleanup

$runtime_staging_sql

$load_body

$end_artifact_cleanup
end;
