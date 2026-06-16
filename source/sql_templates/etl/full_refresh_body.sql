delete from $current_table;

insert into $current_table (
    __SOURCE_COLUMNS__
  , [Row insert datetime]
  , [Row update datetime]
  , [Row delete datetime]
)
select
    __STAGING_SELECT_COLUMNS__
  , @weaver_load_datetime
  , @weaver_load_datetime
  , convert(datetime2(6), '9999-12-31 00:00:00')
from $staging_table as s;
