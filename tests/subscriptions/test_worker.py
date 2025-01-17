import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Generator, Iterable, MutableMapping, Optional, Tuple
from uuid import UUID, uuid1

import pytest
from arroyo import Message, Partition, Topic
from arroyo.backends.local.backend import LocalBroker as Broker
from arroyo.backends.local.storages.memory import MemoryMessageStorage
from arroyo.utils.clock import TestingClock

from snuba import state
from snuba.datasets.factory import get_dataset
from snuba.query.conditions import ConditionFunctions, get_first_level_and_conditions
from snuba.query.matchers import (
    Column,
    FunctionCall,
    Literal,
    MatchResult,
    Pattern,
    String,
)
from snuba.subscriptions.consumer import Tick
from snuba.subscriptions.data import (
    DelegateSubscriptionData,
    LegacySubscriptionData,
    PartitionId,
    SnQLSubscriptionData,
    Subscription,
    SubscriptionData,
    SubscriptionIdentifier,
)
from snuba.subscriptions.scheduler import SubscriptionScheduler
from snuba.subscriptions.store import SubscriptionDataStore
from snuba.subscriptions.worker import (
    SubscriptionTaskResult,
    SubscriptionWorker,
    handle_nan,
)
from snuba.utils.metrics.backends.dummy import DummyMetricsBackend
from snuba.utils.types import Interval
from tests.backends.metrics import Increment, TestingMetricsBackend


class DummySubscriptionDataStore(SubscriptionDataStore):
    def __init__(self) -> None:
        self.__subscriptions: MutableMapping[UUID, SubscriptionData] = {}

    def create(self, key: UUID, data: SubscriptionData) -> None:
        self.__subscriptions[key] = data

    def delete(self, key: UUID) -> None:
        try:
            del self.__subscriptions[key]
        except KeyError:
            pass

    def all(self) -> Iterable[Tuple[UUID, SubscriptionData]]:
        return [*self.__subscriptions.items()]


@dataclass(frozen=True)
class Datetime(Pattern[datetime]):
    """
    Matches one specific datetime.
    """

    value: datetime

    def match(self, node: Any) -> Optional[MatchResult]:
        return MatchResult() if node == self.value else None


@pytest.fixture(
    ids=["Legacy", "SnQL", "Delegate"],
    params=[
        LegacySubscriptionData(
            project_id=1,
            conditions=[],
            aggregations=[["count()", "", "count"]],
            time_window=timedelta(minutes=60),
            resolution=timedelta(minutes=1),
        ),
        SnQLSubscriptionData(
            project_id=1,
            query=("MATCH (events) SELECT count() AS count"),
            time_window=timedelta(minutes=60),
            resolution=timedelta(minutes=1),
        ),
        DelegateSubscriptionData(
            project_id=1,
            conditions=[],
            aggregations=[["count()", "", "count"]],
            query=("MATCH (events) SELECT count() AS count"),
            time_window=timedelta(minutes=60),
            resolution=timedelta(minutes=1),
        ),
    ],
)
def subscription_data(request: Any) -> SubscriptionData:
    assert isinstance(request.param, SubscriptionData)
    return request.param


@pytest.fixture
def subscription_rollout() -> Generator[None, None, None]:
    state.set_config("snql_subscription_rollout_pct", 1.0)
    yield
    state.set_config("snql_subscription_rollout", 0.0)


def test_subscription_worker(subscription_data: SubscriptionData) -> None:
    broker: Broker[SubscriptionTaskResult] = Broker(
        MemoryMessageStorage(), TestingClock()
    )

    result_topic = Topic("subscription-results")

    broker.create_topic(result_topic, partitions=1)

    frequency = timedelta(minutes=1)
    evaluations = 3

    subscription = Subscription(
        SubscriptionIdentifier(PartitionId(0), uuid1()), subscription_data,
    )

    store = DummySubscriptionDataStore()
    store.create(subscription.identifier.uuid, subscription.data)

    metrics = DummyMetricsBackend(strict=True)

    dataset = get_dataset("events")
    worker = SubscriptionWorker(
        dataset,
        ThreadPoolExecutor(),
        {0: SubscriptionScheduler(store, PartitionId(0), timedelta(), metrics)},
        broker.get_producer(),
        result_topic,
        metrics,
    )

    now = datetime(2000, 1, 1)

    tick = Tick(
        offsets=Interval(0, 1),
        timestamps=Interval(now - (frequency * evaluations), now),
    )

    result_futures = worker.process_message(
        Message(Partition(Topic("events"), 0), 0, tick, now)
    )

    assert result_futures is not None and len(result_futures) == evaluations

    # Publish the results.
    worker.flush_batch([result_futures])

    # Check to make sure the results were published.
    # NOTE: This does not cover the ``SubscriptionTaskResultCodec``!
    consumer = broker.get_consumer("group")
    consumer.subscribe([result_topic])

    for i in range(evaluations):
        timestamp = now - frequency * (evaluations - i)

        message = consumer.poll()
        assert message is not None
        assert message.partition.topic == result_topic

        task, future = result_futures[i]
        future_result = request, result = future.result()
        assert message.payload.task.timestamp == timestamp
        assert message.payload == SubscriptionTaskResult(task, future_result)

        # NOTE: The time series extension is folded back into the request
        # body, ideally this would reference the timeseries options in
        # isolation.
        from_pattern = FunctionCall(
            String(ConditionFunctions.GTE),
            (
                Column(None, String("timestamp")),
                Literal(Datetime(timestamp - subscription.data.time_window)),
            ),
        )
        to_pattern = FunctionCall(
            String(ConditionFunctions.LT),
            (Column(None, String("timestamp")), Literal(Datetime(timestamp))),
        )

        condition = request.query.get_condition()
        assert condition is not None

        conditions = get_first_level_and_conditions(condition)

        assert any([from_pattern.match(e) for e in conditions])
        assert any([to_pattern.match(e) for e in conditions])

        assert result == {
            "meta": [{"name": "count", "type": "UInt64"}],
            "data": [{"count": 0}],
        }


def test_subscription_worker_consistent(subscription_data: SubscriptionData) -> None:
    state.set_config("event_subscription_non_consistent_sample_rate", 1)
    broker: Broker[SubscriptionTaskResult] = Broker(
        MemoryMessageStorage(), TestingClock()
    )

    result_topic = Topic("subscription-results")

    broker.create_topic(result_topic, partitions=1)

    frequency = timedelta(minutes=1)
    evaluations = 1

    subscription = Subscription(
        SubscriptionIdentifier(PartitionId(0), uuid1()), subscription_data,
    )

    store = DummySubscriptionDataStore()
    store.create(subscription.identifier.uuid, subscription.data)

    metrics = TestingMetricsBackend()

    dataset = get_dataset("events")
    worker = SubscriptionWorker(
        dataset,
        ThreadPoolExecutor(),
        {
            0: SubscriptionScheduler(
                store, PartitionId(0), timedelta(), DummyMetricsBackend(strict=True)
            )
        },
        broker.get_producer(),
        result_topic,
        metrics,
    )

    now = datetime(2000, 1, 1)

    tick = Tick(
        offsets=Interval(0, 1),
        timestamps=Interval(now - (frequency * evaluations), now),
    )

    worker.process_message(Message(Partition(Topic("events"), 0), 0, tick, now))

    time.sleep(0.1)

    assert (
        len(
            [
                m
                for m in metrics.calls
                if isinstance(m, Increment) and m.name == "consistent"
            ]
        )
        == 1
    )


def test_handle_nan() -> None:
    assert handle_nan({"data": [{"a": float("nan"), "b": None}]}) == {
        "data": [{"a": "nan", "b": None}]
    }
