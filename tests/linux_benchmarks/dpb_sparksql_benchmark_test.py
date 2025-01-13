import json
import sys
from typing import Any
import unittest
from unittest import mock

from absl.testing import flagsaver
from absl.testing import parameterized
import freezegun
from perfkitbenchmarker import dpb_sparksql_benchmark_helper
from perfkitbenchmarker.linux_benchmarks import dpb_sparksql_benchmark
from tests import pkb_common_test_case

PY4J_MOCK = mock.Mock()
PYSPARK_MOCK = mock.Mock()
sys.modules['py4j'] = PY4J_MOCK
sys.modules['pyspark'] = PYSPARK_MOCK

from perfkitbenchmarker.scripts.spark_sql_test_scripts import spark_sql_runner


_TPCH_TABLES = [
    'customer',
    'lineitem',
    'nation',
    'orders',
    'part',
    'partsupp',
    'region',
    'supplier',
]


class DpbSparksqlBenchmarkTest(pkb_common_test_case.PkbCommonTestCase):

  def setUp(self):
    super().setUp()
    self.benchmark_spec_mock = mock.MagicMock()
    self.benchmark_spec_mock.dpb_service = mock.Mock(base_dir='gs://test2')

  @freezegun.freeze_time('2023-03-29')
  @flagsaver.flagsaver(dpb_sparksql_order=['1', '2', '3'])
  def testRunQueriesPower(self):
    self.benchmark_spec_mock.query_dir = 'gs://test'
    self.benchmark_spec_mock.query_streams = (
        dpb_sparksql_benchmark_helper.GetStreams()
    )
    dpb_sparksql_benchmark._RunQueries(self.benchmark_spec_mock)
    self.benchmark_spec_mock.dpb_service.SubmitJob.assert_called_once()
    _, kwargs = self.benchmark_spec_mock.dpb_service.SubmitJob.call_args
    self.assertEqual(
        kwargs['job_arguments'],
        [
            '--sql-scripts-dir',
            'gs://test',
            '--sql-scripts',
            '1,2,3',
            '--report-dir',
            'gs://test2/report-1680048000000',
            '--enable-hive',
            'True',
        ],
    )

  @freezegun.freeze_time('2023-03-29')
  @flagsaver.flagsaver(
      dpb_sparksql_order=['1', '2', '3'], dpb_sparksql_simultaneous=True
  )
  def testRunQueriesThroughput(self):
    self.benchmark_spec_mock.query_dir = 'gs://test'
    self.benchmark_spec_mock.query_streams = (
        dpb_sparksql_benchmark_helper.GetStreams()
    )
    dpb_sparksql_benchmark._RunQueries(self.benchmark_spec_mock)
    self.benchmark_spec_mock.dpb_service.SubmitJob.assert_called_once()
    _, kwargs = self.benchmark_spec_mock.dpb_service.SubmitJob.call_args
    self.assertEqual(
        kwargs['job_arguments'],
        [
            '--sql-scripts-dir',
            'gs://test',
            '--sql-scripts',
            '1',
            '--sql-scripts',
            '2',
            '--sql-scripts',
            '3',
            '--report-dir',
            'gs://test2/report-1680048000000',
            '--enable-hive',
            'True',
        ],
    )

  @freezegun.freeze_time('2023-03-29')
  @flagsaver.flagsaver(dpb_sparksql_streams=['1,2,3', '2,1,3', '3,1,2'])
  def testRunQueriesSimultaneous(self):
    self.benchmark_spec_mock.query_dir = 'gs://test'
    self.benchmark_spec_mock.query_streams = (
        dpb_sparksql_benchmark_helper.GetStreams()
    )
    dpb_sparksql_benchmark._RunQueries(self.benchmark_spec_mock)
    self.benchmark_spec_mock.dpb_service.SubmitJob.assert_called_once()
    _, kwargs = self.benchmark_spec_mock.dpb_service.SubmitJob.call_args
    self.assertEqual(
        kwargs['job_arguments'],
        [
            '--sql-scripts-dir',
            'gs://test',
            '--sql-scripts',
            '1,2,3',
            '--sql-scripts',
            '2,1,3',
            '--sql-scripts',
            '3,1,2',
            '--report-dir',
            'gs://test2/report-1680048000000',
            '--enable-hive',
            'True',
        ],
    )

  def SetupTableMetadataMocks(self):
    staged_metadata: str | None = None

    def _FakeStageMetadata(
        table_metadata: dict[Any, Any],
        storage_service: Any,
        table_metadata_file: Any,
    ):
      nonlocal staged_metadata
      del storage_service, table_metadata_file
      staged_metadata = json.dumps(table_metadata)

    stage_metadata_mock = self.enter_context(
        mock.patch.object(
            dpb_sparksql_benchmark_helper,
            'StageMetadata',
            side_effect=_FakeStageMetadata,
        )
    )
    spark_sql_runner_mock = self.enter_context(
        mock.patch.object(
            spark_sql_runner,
            '_load_file',
            side_effect=lambda *args, **kwargs: staged_metadata,
        )
    )
    return stage_metadata_mock, spark_sql_runner_mock

  @parameterized.named_parameters(
      dict(testcase_name='Default', extra_flags={}, want_format='parquet'),
      dict(
          testcase_name='OrcFormat',
          extra_flags={'dpb_sparksql_data_format': 'orc'},
          want_format='orc',
      ),
  )
  @flagsaver.flagsaver(dpb_sparksql_order=['1', '2', '3'])
  def testRunnerScriptGetTableMetadata(self, extra_flags, want_format):
    # Arrange
    stage_metadata_mock, spark_sql_runner_mock = self.SetupTableMetadataMocks()
    self.benchmark_spec_mock.query_dir = 'gs://test'
    self.benchmark_spec_mock.data_dir = 'gs://datasetbucket'
    self.benchmark_spec_mock.query_streams = (
        dpb_sparksql_benchmark_helper.GetStreams()
    )
    self.benchmark_spec_mock.table_subdirs = list(_TPCH_TABLES)
    if extra_flags:
      self.enter_context(flagsaver.flagsaver(**extra_flags))

    # Act
    dpb_sparksql_benchmark._RunQueries(self.benchmark_spec_mock)
    table_metadata = spark_sql_runner.get_table_metadata(
        mock.MagicMock(), mock.MagicMock()
    )

    # Assert
    stage_metadata_mock.assert_called_once()
    spark_sql_runner_mock.assert_called_once()
    self.assertEqual(
        table_metadata,
        {
            'customer': [want_format, {'path': 'gs://datasetbucket/customer'}],
            'lineitem': [want_format, {'path': 'gs://datasetbucket/lineitem'}],
            'nation': [want_format, {'path': 'gs://datasetbucket/nation'}],
            'orders': [want_format, {'path': 'gs://datasetbucket/orders'}],
            'part': [want_format, {'path': 'gs://datasetbucket/part'}],
            'partsupp': [want_format, {'path': 'gs://datasetbucket/partsupp'}],
            'region': [want_format, {'path': 'gs://datasetbucket/region'}],
            'supplier': [want_format, {'path': 'gs://datasetbucket/supplier'}],
        },
    )

  @parameterized.named_parameters(
      dict(testcase_name='Default', extra_flags={}, want_delim=','),
      dict(
          testcase_name='PipeDelim',
          extra_flags={'dpb_sparksql_csv_delimiter': '|'},
          want_delim='|',
      ),
  )
  @flagsaver.flagsaver(
      dpb_sparksql_order=['1', '2', '3'], dpb_sparksql_data_format='csv'
  )
  def testRunnerScriptGetTableMetadataCsv(self, extra_flags, want_delim):
    # Arrange
    stage_metadata_mock, spark_sql_runner_mock = self.SetupTableMetadataMocks()
    self.benchmark_spec_mock.query_dir = 'gs://test'
    self.benchmark_spec_mock.data_dir = 'gs://datasetbucket'
    self.benchmark_spec_mock.query_streams = (
        dpb_sparksql_benchmark_helper.GetStreams()
    )
    self.benchmark_spec_mock.table_subdirs = list(_TPCH_TABLES)
    if extra_flags:
      self.enter_context(flagsaver.flagsaver(**extra_flags))

    # Act
    dpb_sparksql_benchmark._RunQueries(self.benchmark_spec_mock)
    table_metadata = spark_sql_runner.get_table_metadata(
        mock.MagicMock(), mock.MagicMock()
    )

    # Assert
    stage_metadata_mock.assert_called_once()
    spark_sql_runner_mock.assert_called_once()

    self.assertEqual(
        table_metadata,
        {
            'customer': [
                'csv',
                {
                    'path': 'gs://datasetbucket/customer',
                    'header': 'true',
                    'delimiter': want_delim,
                },
            ],
            'lineitem': [
                'csv',
                {
                    'path': 'gs://datasetbucket/lineitem',
                    'header': 'true',
                    'delimiter': want_delim,
                },
            ],
            'nation': [
                'csv',
                {
                    'path': 'gs://datasetbucket/nation',
                    'header': 'true',
                    'delimiter': want_delim,
                },
            ],
            'orders': [
                'csv',
                {
                    'path': 'gs://datasetbucket/orders',
                    'header': 'true',
                    'delimiter': want_delim,
                },
            ],
            'part': [
                'csv',
                {
                    'path': 'gs://datasetbucket/part',
                    'header': 'true',
                    'delimiter': want_delim,
                },
            ],
            'partsupp': [
                'csv',
                {
                    'path': 'gs://datasetbucket/partsupp',
                    'header': 'true',
                    'delimiter': want_delim,
                },
            ],
            'region': [
                'csv',
                {
                    'path': 'gs://datasetbucket/region',
                    'header': 'true',
                    'delimiter': want_delim,
                },
            ],
            'supplier': [
                'csv',
                {
                    'path': 'gs://datasetbucket/supplier',
                    'header': 'true',
                    'delimiter': want_delim,
                },
            ],
        },
    )

  @flagsaver.flagsaver(
      dpb_sparksql_order=['1', '2', '3'],
      bigquery_tables=[
          'tpcds_1t.customer',
          'tpcds_1t.lineitem',
          'tpcds_1t.nation',
          'tpcds_1t.orders',
          'tpcds_1t.part',
          'tpcds_1t.partsupp',
          'tpcds_1t.region',
          'tpcds_1t.supplier',
      ],
      bigquery_record_format='ARROW',
      dpb_sparksql_data_format='com.google.cloud.spark.bigquery',
  )
  def testRunnerScriptGetTableMetadataBigQuery(self):
    # Arrange
    stage_metadata_mock, spark_sql_runner_mock = self.SetupTableMetadataMocks()
    self.benchmark_spec_mock.query_dir = 'gs://test'
    self.benchmark_spec_mock.data_dir = 'gs://datasetbucket'
    self.benchmark_spec_mock.query_streams = (
        dpb_sparksql_benchmark_helper.GetStreams()
    )
    self.benchmark_spec_mock.table_subdirs = list(_TPCH_TABLES)

    # Act
    dpb_sparksql_benchmark._RunQueries(self.benchmark_spec_mock)
    table_metadata = spark_sql_runner.get_table_metadata(
        mock.MagicMock(), mock.MagicMock()
    )

    # Assert
    stage_metadata_mock.assert_called_once()
    spark_sql_runner_mock.assert_called_once()
    self.assertEqual(
        table_metadata,
        {
            'customer': [
                'com.google.cloud.spark.bigquery',
                {'table': 'tpcds_1t.customer', 'readDataFormat': 'ARROW'},
            ],
            'lineitem': [
                'com.google.cloud.spark.bigquery',
                {'table': 'tpcds_1t.lineitem', 'readDataFormat': 'ARROW'},
            ],
            'nation': [
                'com.google.cloud.spark.bigquery',
                {'table': 'tpcds_1t.nation', 'readDataFormat': 'ARROW'},
            ],
            'orders': [
                'com.google.cloud.spark.bigquery',
                {'table': 'tpcds_1t.orders', 'readDataFormat': 'ARROW'},
            ],
            'part': [
                'com.google.cloud.spark.bigquery',
                {'table': 'tpcds_1t.part', 'readDataFormat': 'ARROW'},
            ],
            'partsupp': [
                'com.google.cloud.spark.bigquery',
                {'table': 'tpcds_1t.partsupp', 'readDataFormat': 'ARROW'},
            ],
            'region': [
                'com.google.cloud.spark.bigquery',
                {'table': 'tpcds_1t.region', 'readDataFormat': 'ARROW'},
            ],
            'supplier': [
                'com.google.cloud.spark.bigquery',
                {'table': 'tpcds_1t.supplier', 'readDataFormat': 'ARROW'},
            ],
        },
    )


if __name__ == '__main__':
  unittest.main()
