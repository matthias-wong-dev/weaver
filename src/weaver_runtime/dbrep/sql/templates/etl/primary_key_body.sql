create table $reject_table as
select
    __STAGING_SELECT_COLUMNS__
  , case
        when $staging_blank_case_predicate then cast('null primary key' as varchar(100))
        when s.[__weaver_pk_row_number] > 1 then cast('duplicate primary key' as varchar(100))
    end as [Rejection reason]
from $staging_table as s
where
    (
        $staging_blank_where_predicate
        or s.[__weaver_pk_row_number] > 1
    );

delete s
from $staging_table as s
where
    (
        $staging_blank_where_predicate
        or s.[__weaver_pk_row_number] > 1
    );

create table $upsert_table as
select
    __STAGING_SELECT_COLUMNS__
  , case when $target_missing_predicate then cast(1 as int) else cast(0 as int) end as [_Is new row]
from $staging_table as s
left join $view_name as t on $staging_target_join
where
    (
        $target_missing_predicate
        or exists (
            select
                __STAGING_EXCEPT_COLUMNS__
            except
            select
                __TARGET_EXCEPT_COLUMNS__
        )
    );

insert into $current_table (
    __SOURCE_COLUMNS__
  , [Row insert datetime]
  , [Row update datetime]
  , [Row delete datetime]
)
select
    __UPSERT_SELECT_COLUMNS__
  , @weaver_load_datetime
  , @weaver_load_datetime
  , convert(datetime2(6), '9999-12-31 00:00:00')
from $upsert_table as u
where u.[_Is new row] = 1;

begin try
    insert into $history_table (
        __HISTORY_COLUMNS__
    )
    select
        __HISTORY_SELECT_COLUMNS__
    from $current_table as c
    inner join $upsert_table as u on $current_upsert_join
    where u.[_Is new row] = 0;

    update c
    set
        __UPDATE_SET_COLUMNS__
    from $current_table as c
    inner join $upsert_table as u on $current_upsert_join
    where u.[_Is new row] = 0;
end try
begin catch
    delete h
    from $history_table as h
    inner join $upsert_table as u on $history_upsert_join
    where u.[_Is new row] = 0
        and h.[Row delete datetime] = @weaver_load_datetime;

    throw;
end catch;

$missing_reconciliation
