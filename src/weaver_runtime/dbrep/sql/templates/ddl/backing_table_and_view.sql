create table $current_table (
$current_columns
);

create table $history_table (
$history_columns
);$post_create_section

create or alter view $view_name as
select
$view_columns
from $current_table;
