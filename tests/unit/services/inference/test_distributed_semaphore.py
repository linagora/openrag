from services.inference.distributed_semaphore import DistributedSemaphore, DistributedSemaphoreActor


class TestDistributedSemaphore:
    def test_default_params(self):
        sem = DistributedSemaphore()
        assert sem._name == "llmSemaphore"
        assert sem._namespace == "openrag"
        assert sem._max_concurrent_ops == 10

    def test_custom_params(self):
        sem = DistributedSemaphore(name="vlm", namespace="test", max_concurrent_ops=5)
        assert sem._name == "vlm"
        assert sem._namespace == "test"
        assert sem._max_concurrent_ops == 5

    def test_actor_class_exists(self):
        assert hasattr(DistributedSemaphoreActor, "remote")
