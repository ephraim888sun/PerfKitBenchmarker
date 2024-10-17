# Copyright 2024 PerfKitBenchmarker Authors. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Benchmark to measure the throughput of a managed AI Model's inference."""

import dataclasses
import logging
import math
import multiprocessing
import statistics
import time
from typing import Any

from absl import flags
from perfkitbenchmarker import benchmark_spec as bm_spec
from perfkitbenchmarker import configs
from perfkitbenchmarker import errors
from perfkitbenchmarker import sample
from perfkitbenchmarker.resources import managed_ai_model


BENCHMARK_NAME = 'ai_model_throughput'
BENCHMARK_CONFIG = """
ai_model_throughput:
  description: >
    Records the throughput of a model.
  ai_model:
    model_name: 'llama2'
    model_size: '7b'
    cloud: 'GCP'
  vm_groups:
    default:
      vm_spec: *default_dual_core
      vm_count: 1
  flags:
    gcloud_scopes: cloud-platform
"""

_STARTING_REQUESTS = flags.DEFINE_integer(
    'ai_starting_requests',
    5,
    'Number of requests to send in parallel at beginning of test.',
)

_MAX_PARALLEL_REQUESTS = flags.DEFINE_integer(
    'ai_max_requests',
    None,
    'Max number of requests to send in parallel before ending the test. Set to'
    ' None or the same number as starting requests to effectively run a QPS'
    ' test at only that value.',
)

_TEST_DURATION = flags.DEFINE_integer(
    'ai_test_duration', 60, 'Number of seconds over which requests are sent.'
)

_BURST_TIME = flags.DEFINE_float(
    'ai_burst_time', 1.0, 'Number of seconds between each burst of requests.'
)

_THROW_ON_CLIENT_ERRORS = flags.DEFINE_bool(
    'ai_throw_on_client_errors',
    False,
    'Whether to throw an exception if the client is not powerful enough to'
    ' send the desired QPS.',
)


_QUEUE_WAIT_TIME = 10 * 60
# Sagemaker times out requests if they take longer than 95 seconds.
_FAIL_LATENCY = 95

_SHARED_REQUEST = 'Why do crabs walk sideways?'


def GetConfig(user_config: dict[Any, Any]) -> dict[Any, Any]:
  """Load and return benchmark config.

  Args:
    user_config: user supplied configuration (flags and config file)

  Returns:
    loaded benchmark configuration
  """
  return configs.LoadConfig(BENCHMARK_CONFIG, user_config, BENCHMARK_NAME)


def Prepare(benchmark_spec: bm_spec.BenchmarkSpec):
  del benchmark_spec


def CheckPrerequisites(benchmark_config):
  del benchmark_config
  if (
      _MAX_PARALLEL_REQUESTS.value
      and _MAX_PARALLEL_REQUESTS.value < _STARTING_REQUESTS.value
  ):
    raise errors.Config.InvalidValue(
        'ai_max_requests must be None or >= ai_starting_requests. Got:'
        f' {_MAX_PARALLEL_REQUESTS.value} as compared to'
        f' {_STARTING_REQUESTS.value}'
    )


@dataclasses.dataclass
class ModelResponse:
  """A response from the model."""

  start_time: float
  end_time: float
  response: str | None = None
  status: int = 0


def Run(benchmark_spec: bm_spec.BenchmarkSpec) -> list[sample.Sample]:
  """Run the example benchmark.

  Args:
    benchmark_spec: The benchmark specification. Contains all data that is
      required to run the benchmark.

  Returns:
    A list of sample.Sample instances.
  """
  logging.info('Running Run phase & finding throughput')
  model = benchmark_spec.ai_model
  assert model
  # Label whether it's the first model or not.
  endpoints = model.ListExistingEndpoints()
  model.metadata.update({'First Model': len(endpoints) == 1})
  # Confirm we can send one request.
  _SendPrompt(model, _SHARED_REQUEST)
  return FindMaxThroughput(model)


def FindMaxThroughput(
    ai_model: managed_ai_model.BaseManagedAiModel,
) -> list[sample.Sample]:
  """Finds the max throughput for the model."""
  logging.info('Finding max throughput for model')
  step = 3
  last_responses = []
  burst_requests = _STARTING_REQUESTS.value
  max_requests = _MAX_PARALLEL_REQUESTS.value or (_STARTING_REQUESTS.value + 1)
  failed_responses = []
  responses = []
  for burst_requests in range(_STARTING_REQUESTS.value, max_requests, step):
    logging.info('Sending %s qps', burst_requests)
    responses = BurstRequestsOverTime(
        ai_model, burst_requests, _TEST_DURATION.value, _BURST_TIME.value
    )
    failed_responses = [
        response
        for response in responses
        if response.status != 0
        or response.end_time - response.start_time > _FAIL_LATENCY
    ]
    if failed_responses:
      logging.info(
          'Reached failure point when trying %s bursts with %s failures',
          burst_requests,
          len(failed_responses),
      )
      break
    last_responses = responses
  if not last_responses:
    logging.warning(
        'The very first QPS tried had errors. Probably a smaller staring'
        ' QPS needs to be chosen.',
    )
    return _AggregateResponses(
        responses, failed_responses, ai_model, _STARTING_REQUESTS.value
    )
  last_successful_bursts = burst_requests
  if failed_responses:
    # We just failed, so output results from the last successful QPS.
    last_successful_bursts = burst_requests - step
  else:
    logging.warning(
        'Reached max burst value of %s without failures. Ending the test &'
        ' outputting results from the highest run QPS.',
        last_successful_bursts,
    )
  samples = _AggregateResponses(
      last_responses, failed_responses, ai_model, last_successful_bursts
  )
  assert samples
  metadata = samples[0].metadata
  samples.append(
      sample.Sample(
          'max_throughput',
          last_successful_bursts / _BURST_TIME.value,
          'count',
          metadata,
      )
  )
  return samples


def _AggregateResponses(
    responses: list[ModelResponse],
    failed_responses: list[ModelResponse],
    model: managed_ai_model.BaseManagedAiModel,
    burst_requests: int,
) -> list[sample.Sample]:
  """Aggregates the responses into samples."""
  successful_durations = [
      response.end_time - response.start_time for response in responses
  ]
  logging.info('Response durations dump: %s', successful_durations)
  failed_durations = [
      response.end_time - response.start_time for response in failed_responses
  ]
  logging.info('Failed response durations dump: %s', failed_durations)
  metadata = model.GetResourceMetadata()
  effective_qps = burst_requests / _BURST_TIME.value
  metadata.update({
      'parallel_requests': burst_requests,
      'test_duration': _TEST_DURATION.value,
      'burst_time': _BURST_TIME.value,
      'effective_qps': effective_qps,
  })
  samples = []
  if failed_durations:
    samples.append(
        sample.Sample(
            'failure_median_response_time',
            statistics.median(failed_durations),
            'seconds',
            metadata,
        )
    )
    samples.append(
        sample.Sample(
            'num_failures',
            len(failed_durations),
            'count',
            metadata,
        )
    )
  if not successful_durations:
    return samples
  samples.append(
      sample.Sample(
          'success_rate',
          len(successful_durations)
          / (len(successful_durations) + len(failed_durations))
          * 100.0,
          'percent',
          metadata,
      )
  )
  samples.append(
      sample.Sample(
          'num_responses',
          len(responses),
          'count',
          metadata,
      )
  )
  samples.append(
      sample.Sample(
          'median_response_time',
          statistics.median(successful_durations),
          'seconds',
          metadata,
      )
  )
  samples.append(
      sample.Sample(
          'mean_response_time',
          statistics.mean(successful_durations),
          'seconds',
          metadata,
      )
  )
  return samples


def SendParallelRequests(
    ai_model: managed_ai_model.BaseManagedAiModel,
    requests: int,
    output_queue: multiprocessing.Queue,
) -> list[multiprocessing.Process]:
  """Sends X requests to the model in parallel."""
  logging.info('Sending %s requests in parallel', requests)
  processes = []
  for _ in range(requests):
    p = multiprocessing.Process(
        target=TimePromptsForModel, args=(ai_model, output_queue)
    )
    processes.append(p)
    p.start()
  _UnitTestIdleTime()
  return processes


def _UnitTestIdleTime():
  """Sleeps in unit test."""
  pass


def _EncounterClientError(error_msg):
  """Throws or logs a client error."""
  if _THROW_ON_CLIENT_ERRORS.value:
    raise errors.Benchmarks.RunError(error_msg)
  logging.warning(error_msg)


def BurstRequestsOverTime(
    ai_model: managed_ai_model.BaseManagedAiModel,
    burst_requests: int,
    total_duration: int,
    time_between_bursts: float = 1.0,
) -> list[ModelResponse]:
  """Sends X requests to the model in parallel over total_duration seconds."""
  start_time = time.time()
  goal_bursts = math.floor(total_duration / time_between_bursts)
  logging.info(
      'Starting to send %s requests every %s seconds over %s duration %s times',
      burst_requests,
      time_between_bursts,
      total_duration,
      goal_bursts,
  )
  output_queue = multiprocessing.Queue()
  processes = []
  for _ in range(goal_bursts):
    process_start_time = time.time()
    processes += SendParallelRequests(ai_model, burst_requests, output_queue)
    process_startup_duration = time.time() - process_start_time
    if process_startup_duration > time_between_bursts:
      elapsed_time = time.time() - start_time
      _EncounterClientError(
          f'After running for {elapsed_time} seconds, the client took'
          f' {process_startup_duration} seconds to send'
          f' {burst_requests} requests, which is more than the'
          f' {time_between_bursts} seconds needed to meet QPS. This means the'
          ' client is not powerful enough & client with more CPUs should be'
          ' used.'
      )
    # Wait to send next burst.
    while time.time() - process_start_time < time_between_bursts:
      time.sleep(0.1)
  logging.info('Waiting for all queued results')

  def EmptyQueue():
    results = []
    queue_start_time = time.time()
    queue_duration = 0
    while not output_queue.empty():
      results.append(output_queue.get())
      queue_duration = time.time() - queue_start_time
      if queue_duration > _QUEUE_WAIT_TIME:
        _EncounterClientError(
            'Waited more than %s seconds for the queue to empty. Exiting, but'
            ' some data may have been dropped.' % _QUEUE_WAIT_TIME,
        )
        break
    logging.info(
        'All %s queue results collected in: %s.',
        len(results),
        queue_duration,
    )
    return results

  results = EmptyQueue()
  process_start_time = time.time()
  process_duration = 0
  for p in processes:
    p.join(_FAIL_LATENCY)
    process_duration = time.time() - process_start_time
    if process_duration > _FAIL_LATENCY:
      _EncounterClientError(
          f'Waited more than {_FAIL_LATENCY} seconds for processes to join.'
          ' Exiting, but some data may have been dropped.'
      )
      break
  logging.info(
      'All processes finished joining in %s seconds.',
      process_duration,
  )
  if not results:
    results = EmptyQueue()
  logging.info('Dumping all response results: %s', results)
  expected_results = goal_bursts * burst_requests
  if len(results) < expected_results:
    logging.info(
        'Theoretically started %s results but only got %s from %s processes.'
        ' Exact reason is unknown, but this is not entirely unexpected.',
        expected_results,
        len(results),
        len(processes),
    )
  return results


def TimePromptsForModel(
    ai_model: managed_ai_model.BaseManagedAiModel,
    output_queue: multiprocessing.Queue,
):
  """Times the prompts for the model & stores timing in the output queue."""
  start_time = time.time()
  status = 0
  response = None
  try:
    response = _SendPrompt(ai_model, _SHARED_REQUEST)
    end_time = time.time()
  except errors.Resource.GetError as ex:
    end_time = time.time()
    logging.info('Failed to send prompt: %s', ex)
    status = 1
  output_queue.put(ModelResponse(start_time, end_time, response, status))


def _SendPrompt(
    ai_model: managed_ai_model.BaseManagedAiModel,
    prompt: str,
):
  """Sends a prompt to the model and prints the response."""
  responses = ai_model.SendPrompt(
      prompt=prompt, max_tokens=512, temperature=0.8
  )
  for response in responses:
    logging.info('Sent request & got response: %s', response)


def Cleanup(benchmark_spec: bm_spec.BenchmarkSpec):
  """Cleanup resources to their original state.

  Args:
    benchmark_spec: The benchmark specification. Contains all data that is
      required to run the benchmark.
  """
  logging.info('Running Cleanup phase of the benchmark')
  del benchmark_spec
