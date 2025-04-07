import logging
from abc import ABC
from pathlib import Path
from typing import Iterator, List, Optional

import polars as pl
from tqdm import tqdm

from ..data import Patient
from ..tasks import BaseTask
from ..utils import load_yaml
from .sample_dataset import SampleDataset

logger = logging.getLogger(__name__)


class BaseDataset(ABC):
    """Abstract base class for all PyHealth datasets.

    Attributes:
        root (Path): The root directory where dataset files are stored.
        tables (List[str]): List of table names to load.
        dataset_name (str): Name of the dataset.
        config (dict): Configuration loaded from a YAML file.
        global_event_df (pl.LazyFrame): The global event data frame.
    """

    def __init__(
        self,
        root: str,
        tables: List[str],
        dataset_name: Optional[str] = None,
        config_path: Optional[str] = None,
    ):
        """Initializes the BaseDataset.

        Args:
            root (str): The root directory where dataset files are stored.
            tables (List[str]): List of table names to load.
            dataset_name (Optional[str]): Name of the dataset. Defaults to class name.
            config_path (Optional[str]): Path to the configuration YAML file.
        """
        self.root = Path(root)
        self.tables = tables
        self.dataset_name = dataset_name or self.__class__.__name__
        self.config = load_yaml(config_path)
        logger.info(
            f"Initializing {self.dataset_name} dataset from {self.root}"
        )

        self.global_event_df = self.load_data()

        # Cached attributes
        self._collected_global_event_df = None
        self._unique_patient_ids = None

    @property
    def collected_global_event_df(self) -> pl.DataFrame:
        """Collects and returns the global event data frame.

        Returns:
            pl.DataFrame: The collected global event data frame.
        """
        if self._collected_global_event_df is None:
            self._collected_global_event_df = self.global_event_df.collect()
        return self._collected_global_event_df

    def load_data(self) -> pl.LazyFrame:
        """Loads data from the specified tables.

        Returns:
            pl.LazyFrame: A concatenated lazy frame of all tables.
        """
        frames = [self.load_table(table) for table in self.tables]
        return pl.concat(frames, how="diagonal")

    def load_table(self, table_name: str) -> pl.LazyFrame:
        """Loads a table and processes joins if specified.

        Args:
            table_name (str): The name of the table to load.

        Returns:
            pl.LazyFrame: The processed lazy frame for the table.

        Raises:
            ValueError: If the table is not found in the config.
            FileNotFoundError: If the CSV file for the table or join is not found.
        """
        if table_name not in self.config:
            raise ValueError(f"Table {table_name} not found in config")

        table_cfg = self.config[table_name]
        csv_path = self.root / table_cfg["file_path"]

        if not csv_path.exists():
            raise FileNotFoundError(f"CSV not found: {csv_path}")

        logger.info(f"Scanning table: {table_name} from {csv_path}")
        df = pl.scan_csv(csv_path, infer_schema=False)

        # Handle joins
        for join_cfg in table_cfg.get("join", []):
            other_csv_path = self.root / join_cfg["file_path"]
            if not other_csv_path.exists():
                raise FileNotFoundError(
                    f"Join CSV not found: {other_csv_path}"
                )

            join_df = pl.scan_csv(other_csv_path, infer_schema=False)
            join_key = join_cfg["on"]
            columns = join_cfg["columns"]
            how = join_cfg.get("how", "left")

            df = df.join(
                join_df.select([join_key] + columns), on=join_key, how=how
            )

        patient_id_col = table_cfg["patient_id"]
        timestamp_col = table_cfg.get("timestamp")
        attribute_cols = table_cfg.get("attributes", [])

        # Timestamp expression
        timestamp_expr = (
            pl.col(timestamp_col).str.strptime(
                pl.Datetime, "%Y-%m-%d %H:%M:%S", strict=False
            )
            if timestamp_col
            else pl.lit(None, dtype=pl.Datetime)
        )

        # Prepare base event columns
        base_columns = [
            pl.col(patient_id_col).cast(pl.Utf8).alias("patient_id"),
            pl.lit(table_name).cast(pl.Utf8).alias("event_type"),
            timestamp_expr.cast(pl.Datetime).alias("timestamp"),
        ]

        # Flatten attribute columns with event_type prefix
        attribute_columns = [
            pl.col(attr).alias(f"{table_name}/{attr}")
            for attr in attribute_cols
        ]

        event_frame = df.select(base_columns + attribute_columns)

        return event_frame

    @property
    def unique_patient_ids(self) -> List[str]:
        """Returns a list of unique patient IDs.

        Returns:
            List[str]: List of unique patient IDs.
        """
        if self._unique_patient_ids is None:
            self._unique_patient_ids = (
                self.collected_global_event_df.select("patient_id")
                .unique()
                .to_series()
                .to_list()
            )
        return self._unique_patient_ids

    def get_patient(self, patient_id: str) -> Patient:
        """Retrieves a Patient object for the given patient ID.

        Args:
            patient_id (str): The ID of the patient to retrieve.

        Returns:
            Patient: The Patient object for the given ID.

        Raises:
            AssertionError: If the patient ID is not found in the dataset.
        """
        assert (
            patient_id in self.unique_patient_ids
        ), f"Patient {patient_id} not found in dataset"
        df = self.collected_global_event_df.filter(
            pl.col("patient_id") == patient_id
        )
        return Patient(patient_id=patient_id, data_source=df)

    def iter_patients(self) -> Iterator[Patient]:
        """Yields Patient objects for each unique patient in the dataset.

        Yields:
            Iterator[Patient]: An iterator over Patient objects.
        """
        grouped = self.collected_global_event_df.group_by("patient_id")

        for patient_id, patient_df in grouped:
            yield Patient(patient_id=patient_id, data_source=patient_df)

    def stats(self) -> None:
        """Prints statistics about the dataset."""
        df = self.collected_global_event_df
        print(f"Dataset: {self.dataset_name}")
        print(f"Number of patients: {df['patient_id'].n_unique()}")
        print(f"Number of events: {df.height}")

    @property
    def default_task(self) -> Optional[BaseTask]:
        """Returns the default task for the dataset.

        Returns:
            Optional[BaseTask]: The default task, if any.
        """
        return None

    def set_task(self, task: Optional[BaseTask] = None) -> SampleDataset:
        """Processes the base dataset to generate the task-specific sample dataset.

        Args:
            task (Optional[BaseTask]): The task to set. Uses default task if None.

        Returns:
            SampleDataset: The generated sample dataset.

        Raises:
            AssertionError: If no default task is found and task is None.
        """
        if task is None:
            assert self.default_task is not None, "No default tasks found"
            task = self.default_task

        logger.info(f"Setting task for {self.dataset_name} base dataset...")

        samples = []
        for patient in tqdm(
            self.iter_patients(), desc=f"Generating samples for {task.task_name}"
        ):
            samples.extend(task(patient))
        sample_dataset = SampleDataset(
            samples,
            input_schema=task.input_schema,
            output_schema=task.output_schema,
            dataset_name=self.dataset_name,
            task_name=task,
        )
        return sample_dataset
