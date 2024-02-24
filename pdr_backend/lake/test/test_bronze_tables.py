from enforce_typing import enforce_types
from pdr_backend.lake.test.resources import _gql_data_factory
from pdr_backend.lake.table_bronze_pdr_slots import (
    get_bronze_pdr_slots_table,
    bronze_pdr_slots_table_name,
    bronze_pdr_slots_schema,
)
from pdr_backend.lake.table_bronze_pdr_predictions import (
    get_bronze_pdr_predictions_table,
    bronze_pdr_predictions_table_name,
    bronze_pdr_predictions_schema,
)
from pdr_backend.lake.table_pdr_predictions import (
    predictions_schema,
    predictions_table_name,
)
from pdr_backend.lake.table_pdr_truevals import truevals_schema, truevals_table_name
from pdr_backend.lake.table_pdr_payouts import payouts_schema, payouts_table_name
from pdr_backend.lake.table_pdr_slots import slots_schema, slots_table_name
from pdr_backend.lake.table import Table


@enforce_types
def test_bronze_tables_creation(
    _gql_datafactory_etl_payouts_df,
    _gql_datafactory_etl_predictions_df,
    _gql_datafactory_etl_truevals_df,
    _gql_datafactory_etl_slots_df,
    tmpdir,
):
    # please note date, including Nov 1st
    st_timestr = "2023-11-01_0:00"
    fin_timestr = "2023-11-07_0:00"

    ppss, _ = _gql_data_factory(
        tmpdir,
        "binanceus ETH/USDT h 5m",
        st_timestr,
        fin_timestr,
    )

    gql_tables = {
        "pdr_predictions": Table(predictions_table_name, predictions_schema, ppss),
        "pdr_truevals": Table(truevals_table_name, truevals_schema, ppss),
        "pdr_payouts": Table(payouts_table_name, payouts_schema, ppss),
        "pdr_slots": Table(slots_table_name, slots_schema, ppss),
        "bronze_pdr_predictions": Table(
            bronze_pdr_predictions_table_name, bronze_pdr_predictions_schema, ppss
        ),
        "bronze_pdr_slots": Table(
            bronze_pdr_slots_table_name, bronze_pdr_slots_schema, ppss
        ),
    }

    gql_tables["pdr_predictions"].df = _gql_datafactory_etl_predictions_df
    gql_tables["pdr_truevals"].df = _gql_datafactory_etl_truevals_df
    gql_tables["pdr_payouts"].df = _gql_datafactory_etl_payouts_df
    gql_tables["pdr_slots"].df = _gql_datafactory_etl_slots_df

    # Create bronze predictions table
    gql_tables["bronze_pdr_predictions"] = get_bronze_pdr_predictions_table(
        gql_tables, ppss
    )

    # Validate bronze_pdr_prediction_table is correct, and as expected
    assert (
        gql_tables["bronze_pdr_predictions"].df.schema == bronze_pdr_predictions_schema
    )
    assert len(gql_tables["bronze_pdr_predictions"].df) == 6

    # Create bronze slots table
    gql_tables["bronze_pdr_slots"] = get_bronze_pdr_slots_table(gql_tables, ppss)
    assert gql_tables["bronze_pdr_slots"].df.schema == bronze_pdr_slots_schema
    assert len(gql_tables["bronze_pdr_slots"].df) == 7

    # Get predictions data from predictions table for slots within slots table
    slots_with_predictions_df = gql_tables["bronze_pdr_slots"].df.join(
        gql_tables["bronze_pdr_predictions"].df.select(
            ["slot", "user", "payout", "predvalue"]
        ),
        on=["slot"],
        how="left",
    )

    users = slots_with_predictions_df["user"].to_list()
    assert len(users) == 7

    predvalues = slots_with_predictions_df["predvalue"].to_list()
    assert len(predvalues) == 7

    payouts = slots_with_predictions_df["payout"].to_list()
    assert len(payouts) == 7

    # filter data frame by slot
    filtered_by_slot = slots_with_predictions_df.filter(
        slots_with_predictions_df["slot"] == 1698951600
    )

    # create lists of payouts and users for selected slot
    payouts_for_slot = filtered_by_slot["payout"].to_list()
    users_for_slot = filtered_by_slot["user"].to_list()

    assert len(payouts_for_slot) == 1
    assert int(payouts_for_slot[0]) == 10

    assert len(users_for_slot) == len(payouts_for_slot)
    assert users_for_slot[0] == "0xd2a24cb4ff2584bad80ff5f109034a891c3d88dd"
