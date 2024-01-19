import os
from typing import Callable, Dict

import polars as pl
from enforce_typing import enforce_types

from pdr_backend.lake.plutil import has_data, newest_ut
from pdr_backend.lake.table_pdr_predictions import (
    get_pdr_predictions_df,
    predictions_schema,
)
from pdr_backend.lake.table_pdr_subscriptions import (
    get_pdr_subscriptions_df,
    subscriptions_schema,
)
from pdr_backend.subgraph.subgraph_predictions import get_all_contract_ids_by_owner
from pdr_backend.ppss.ppss import PPSS
from pdr_backend.util.networkutil import get_sapphire_postfix
from pdr_backend.util.timeutil import current_ut_ms, pretty_timestr


@enforce_types
class GQLDataFactory:
    """
    Roles:
    - From each GQL API, fill >=1 gql_dfs -> parquet files data lake
    - From gql_dfs, calculate other dfs and stats
    - All timestamps, after fetching, are transformed into milliseconds wherever appropriate

    Finally:
       - "timestamp" values are ut: int is unix time, UTC, in ms (not s)
       - "datetime" values ares python datetime.datetime, UTC
    """

    def __init__(self, ppss: PPSS):
        self.ppss = ppss

        # filter by feed contract address
        network = get_sapphire_postfix(ppss.web3_pp.network)
        contract_list = get_all_contract_ids_by_owner(
            owner_address=self.ppss.web3_pp.owner_addrs,
            network=network,
        )
        contract_list = [f.lower() for f in contract_list]

        # configure all tables that will be recorded onto lake
        self.record_config = {
            "pdr_predictions": {
                "fetch_fn": get_pdr_predictions_df,
                "schema": predictions_schema,
                "config": {
                    "contract_list": contract_list,
                },
            },
            "pdr_subscriptions": {
                "fetch_fn": get_pdr_subscriptions_df,
                "schema": subscriptions_schema,
                "config": {
                    "contract_list": contract_list,
                },
            },
        }

    def get_gql_dfs(self) -> Dict[str, pl.DataFrame]:
        """
        @description
          Get historical dataframes across many feeds and timeframes.

        @return
          predictions_df -- *polars* Dataframe. See class docstring
        """
        print("Get predictions data across many feeds and timeframes.")

        # Ss_timestamp is calculated dynamically if ss.fin_timestr = "now".
        # But, we don't want fin_timestamp changing as we gather data here.
        # To solve, for a given call to this method, we make a constant fin_ut
        fin_ut = self.ppss.lake_ss.fin_timestamp

        print(f"  Data start: {pretty_timestr(self.ppss.lake_ss.st_timestamp)}")
        print(f"  Data fin: {pretty_timestr(fin_ut)}")

        self._update(fin_ut)
        gql_dfs = self._load_parquet(fin_ut)

        print("Get historical data across many subgraphs. Done.")

        # postconditions
        assert len(gql_dfs.values()) > 0
        for df in gql_dfs.values():
            assert isinstance(df, pl.DataFrame)

        return gql_dfs

    def _update(self, fin_ut: int):
        """
        @description
            Iterate across all gql queries and update their parquet files:
            - Predictoors
            - Slots
            - Claims

            Improve this by:
            1. Break out raw data from any transformed/cleaned data
            2. Integrate other queries and summaries
            3. Integrate config/pp if needed
        @arguments
            fin_ut -- a timestamp, in ms, in UTC
        """

        for k, record in self.record_config.items():
            filename = self._parquet_filename(k)
            print(f"      filename={filename}")

            st_ut = self._calc_start_ut(filename)
            print(f"      Aim to fetch data from start time: {pretty_timestr(st_ut)}")
            if st_ut > min(current_ut_ms(), fin_ut):
                print("      Given start time, no data to gather. Exit.")
                continue

            # to satisfy mypy, get an explicit function pointer
            do_fetch: Callable[[str, int, int, Dict], pl.DataFrame] = record["fetch_fn"]

            # call the function
            print(f"    Fetching {k}")
            df = do_fetch(self.ppss.web3_pp.network, st_ut, fin_ut, record["config"])

            # postcondition
            if len(df) > 0:
                assert df.schema == record["schema"]

                # save to parquet
                self._save_parquet(filename, df)

    def _calc_start_ut(self, filename: str) -> int:
        """
        @description
            Calculate start timestamp, reconciling whether file exists and where
            its data starts. If file exists, you can only append to end.

        @arguments
          filename - parquet file with data. May or may not exist.

        @return
          start_ut - timestamp (ut) to start grabbing data for (in ms)
        """
        if not os.path.exists(filename):
            print("      No file exists yet, so will fetch all data")
            return self.ppss.lake_ss.st_timestamp

        print("      File already exists")
        if not has_data(filename):
            print("      File has no data, so delete it")
            os.remove(filename)
            return self.ppss.lake_ss.st_timestamp

        file_utN = newest_ut(filename)
        return file_utN + 1000

    def _load_parquet(self, fin_ut: int) -> Dict[str, pl.DataFrame]:
        """
        @arguments
          fin_ut -- finish timestamp

        @return
          gql_dfs -- dict of [gql_filename] : df
            Where df has columns=GQL_COLS+"datetime", and index=timestamp
        """
        print("  Load parquet.")
        st_ut = self.ppss.lake_ss.st_timestamp

        dfs: Dict[str, pl.DataFrame] = {}  # [parquet_filename] : df

        for k, record in self.record_config.items():
            filename = self._parquet_filename(k)
            print(f"      filename={filename}")

            # load all data from file
            # check if file exists
            # if file doesn't exist, return an empty dataframe with the expected schema
            if os.path.exists(filename):
                df = pl.read_parquet(filename)
            else:
                df = pl.DataFrame(schema=record["schema"])

            df = df.filter(
                (pl.col("timestamp") >= st_ut) & (pl.col("timestamp") <= fin_ut)
            )

            # postcondition
            assert df.schema == record["schema"]
            dfs[k] = df

        return dfs

    def _parquet_filename(self, filename_str: str) -> str:
        """
        @description
          Computes the lake-path for the parquet file.

        @arguments
          filename_str -- eg "subgraph_predictions"

        @return
          parquet_filename -- name for parquet file.
        """
        basename = f"{filename_str}.parquet"
        filename = os.path.join(self.ppss.lake_ss.parquet_dir, basename)
        return filename

    @enforce_types
    def _save_parquet(self, filename: str, df: pl.DataFrame):
        """write to parquet file
        parquet only supports appending via the pyarrow engine
        """

        # precondition
        assert "timestamp" in df.columns and df["timestamp"].dtype == pl.Int64
        assert len(df) > 0
        if len(df) > 1:
            assert (
                df.head(1)["timestamp"].to_list()[0]
                <= df.tail(1)["timestamp"].to_list()[0]
            )

        if os.path.exists(filename):  # "append" existing file
            cur_df = pl.read_parquet(filename)
            df = pl.concat([cur_df, df])

            # drop duplicates
            duplicate_rows = df.filter(pl.struct("ID").is_duplicated())
            if len(duplicate_rows) > 0:
                print(
                    f"Duplicate rows found. Dropping {len(duplicate_rows)} rows: {duplicate_rows}"
                )
            df = df.group_by("ID").first()
            
            df.write_parquet(filename)
            n_new = df.shape[0] - cur_df.shape[0]
            print(f"  Just appended {n_new} df rows to file {filename}")
        else:  # write new file
            df.write_parquet(filename)
            print(f"  Just saved df with {df.shape[0]} rows to new file {filename}")

    @enforce_types
    def _post_fetch_process_truevals(
        self, dfs: Dict[str, pl.DataFrame]
    ) -> pl.DataFrame:
        """
        @description
          Merge the fetched data with the existing data in the parquet file.
          This is where we do any post-fetch transformations.
        """
        st_ut = self.ppss.lake_ss.st_timestamp / 1000
        fin_ut = self.ppss.lake_ss.fin_timestamp / 1000

        # Work 1: update prediction based on truevals
        # process recent truevals => update predictions
        # fetch recent truevals added
        truevals_df = dfs["pdr_truevals"].filter(
            (pl.col("timestamp") >= st_ut) & (pl.col("timestamp") <= fin_ut)
        )
        truevals_ids = truevals_df["ID"].to_list()

        # find the predictions we'll be updating
        print(f"post_truevals pdr_truevals: {truevals_ids}")
        predictions_df = (
            dfs["pdr_predictions"]
            .filter((pl.col("truevalue_id").is_in(truevals_ids)))
            .drop(["truevalue"])
        )

        # update specific predictions
        predictions_df = (
            predictions_df.join(
                truevals_df, left_on=["truevalue_id"], right_on=["ID"], how="left"
            )
            .with_columns(
                [
                    pl.col("truevalue").alias("truevalue"),
                ]
            )
            .select(
                [
                    "ID",
                    "truevalue_id",
                    "contract",
                    "pair",
                    "timeframe",
                    "prediction",
                    "stake",
                    "truevalue",
                    "timestamp",
                    "source",
                    "payout",
                    "slot",
                    "user",
                ]
            )
        )

        # save out updated predictions
        # filename = self._parquet_filename("pdr_predictions")
        # self._save_parquet(filename, predictions_df)
        predictions_df.write_parquet("pdr_predictions.parquet")

    @enforce_types
    def _post_fetch_process_payouts(self, dfs: Dict[str, pl.DataFrame]) -> pl.DataFrame:
        """
        @description
          Merge the fetched data with the existing data in the parquet file.
          This is where we do any post-fetch transformations.
        """
        st_ut = self.ppss.lake_ss.st_timestamp / 1000
        fin_ut = self.ppss.lake_ss.fin_timestamp / 1000

        # Work 2: update prediction based on payouts
        # process recent payouts => update predictions
        # fetch recent payouts added
        payouts_df = dfs["pdr_payouts"].filter(
            (pl.col("timestamp") >= st_ut) & (pl.col("timestamp") <= fin_ut)
        )
        payouts_ids = payouts_df["ID"].to_list()

        # find all predictions that we'll be updating
        predictions_df = (
            dfs["pdr_predictions"]
            .filter((pl.col("ID").is_in(payouts_ids)))
            .drop(["payout", "prediction"])
        )

        # update specific predictions
        predictions_df = (
            predictions_df.join(payouts_df, on="ID", how="left")
            .with_columns(
                [
                    pl.col("predvalue").alias("prediction"),
                ]
            )
            .select(
                [
                    "ID",
                    "truevalue_id",
                    "contract",
                    "pair",
                    "timeframe",
                    "prediction",
                    "stake",
                    "truevalue",
                    "timestamp",
                    "source",
                    "payout",
                    "slot",
                    "user",
                ]
            )
        )

        # save out updated predictions
        # filename = self._parquet_filename("pdr_predictions")
        # self._save_parquet(filename, predictions_df)
        predictions_df.write_parquet("pdr_predictions.parquet")
